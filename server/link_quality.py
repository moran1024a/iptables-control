"""Low-rate measurements of the applied suffix and its adjacent segments."""
import asyncio
import hashlib
import json
import math
import secrets

import pools

PROTOCOLS = ('tcp-connect-v1', 'udp-echo-v1', 'icmp-echo-v1')
SCHEMA = """
CREATE TABLE IF NOT EXISTS link_quality_samples (
 link_id INTEGER NOT NULL REFERENCES links(id) ON DELETE CASCADE,
 section TEXT NOT NULL, protocol TEXT NOT NULL, slot INTEGER NOT NULL,
 status TEXT NOT NULL, rtt REAL, error TEXT NOT NULL DEFAULT '',
 resolved_ip TEXT NOT NULL DEFAULT '', path_key TEXT NOT NULL, path_label TEXT NOT NULL,
 PRIMARY KEY(link_id,section,protocol,slot)
);
CREATE INDEX IF NOT EXISTS link_quality_age ON link_quality_samples(slot);
"""


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()[:24]


def sections(s, c, link, nodes):
    plan = json.loads(link['plan'])
    selected = json.loads(link['selected'])
    if link['job']:
        previous = json.loads(link['job']).get('previous')
        if previous:
            plan, selected = previous['plan'], previous['selected']
    chain = c.execute('SELECT * FROM chains WHERE id=?', (link['chain_id'],)).fetchone()
    target = c.execute('SELECT * FROM targets WHERE id=?', (chain['target_id'] if chain else plan['target'],)).fetchone()
    hops = [dict(r) for r in c.execute('SELECT * FROM hops WHERE chain_id=? ORDER BY seq', (link['chain_id'],))]
    by_node = {h['node_id']: h for h in hops}
    stages = [(st['id'], st['node_id'] if st['kind'] == 'node' else selected.get(st['id'])) for st in plan['stages']]
    actual = [n for _, n in stages if n]
    expected = ([plan['entry']] if plan['entry'] else []) + actual
    ready = bool(chain and target and [h['node_id'] for h in hops] == expected)
    name = lambda nid: 'direct' if nid == 0 else nodes[nid]['name'] if nid in nodes else '未选择'
    target_name = target['name'] if target else '目标'
    descriptors = [('full', '全段', actual[0] if actual else None, None, actual)]
    for i, (sid, nid) in enumerate(stages):
        peer = stages[i + 1][1] if i + 1 < len(stages) else None
        descriptors.append(('stage:' + sid, '子段', nid, peer, [nid, peer] if i + 1 < len(stages) else [nid]))
    result = []
    for sid, kind, source, peer, path in descriptors:
        full = sid == 'full'
        terminal = full or sid == ('stage:' + stages[-1][0] if stages else None)
        label = ' → '.join([name(n) for n in path] + ([target_name] if terminal else []))
        if full and not actual:
            label = '无中间节点 → ' + target_name
        probes = []
        for protocol in PROTOCOLS:
            spec = dict(link_id=link['id'], section=sid, protocol=protocol, source=source,
                        target=peer, host='', port=0, rule_id=0, route=path, path_label=label, reason=None)
            reason = None
            if link['state'] in ('draft', 'deleting') or not ready:
                reason = ('waiting', '等待链路生效')
            elif link['job']:
                reason = ('waiting', '链路切换中')
            elif not source or (not terminal and not peer):
                reason = ('unknown', 'direct' if 0 in path else '无可探测的中间节点')
            elif terminal:
                h = by_node[source]
                next_hop = next((x for x in hops if x['seq'] == h['seq'] + 1), None)
                spec.update(host=nodes[next_hop['node_id']]['addr'] if next_hop else target['host'],
                            port=next_hop['port'] if next_hop else target['port'], rule_id=h['id'], target=None)
                if protocol == 'udp-echo-v1':
                    reason = ('unknown', '目标未提供 UDP 回显')
                elif protocol == 'icmp-echo-v1' and next_hop:
                    reason = ('unknown', 'ICMP 无法沿端口转发路径探测')
            elif protocol == 'tcp-connect-v1':
                reason = ('unknown', '节点间使用 UDP / ICMP 探测')
            else:
                spec.update(host=nodes[peer]['addr'], port=nodes[peer]['quality_port'] if protocol == 'udp-echo-v1' else 0)
            # Include every downstream address/port, so old in-flight results cannot
            # be attached to a new route whose first endpoint happens to be the same.
            identity = dict(source=source, target=peer, host=spec['host'], port=spec['port'],
                            rule_id=spec['rule_id'], protocol=protocol,
                            path=[(n, nodes[n]['addr'], by_node.get(n, {}).get('port')) for n in path if n in nodes],
                            endpoint=(target['id'], target['host'], target['port']) if terminal and target else None)
            spec.update(path_key=fingerprint(identity), reason=reason)
            probes.append(spec)
        result.append(dict(id=sid, kind=kind, label=label, probes=probes))
    return result


def record(c, spec, now, status, rtt=None, error='', resolved_ip=''):
    c.execute('INSERT OR IGNORE INTO link_quality_samples VALUES (?,?,?,?,?,?,?,?,?,?)',
              (spec['link_id'], spec['section'], spec['protocol'], now, status, rtt,
               str(error)[:300], str(resolved_ip)[:64], spec['path_key'], spec['path_label']))


class Monitor:
    def __init__(self, parent):
        self.parent, self.hub = parent, parent.hub
        self.plans, self.nodes, self.pending, self.last = {}, {}, {}, {}

    @staticmethod
    def key(spec):
        return spec['link_id'], spec['section'], spec['protocol']

    def reason(self, spec, nodes=None):
        nodes = self.nodes if nodes is None else nodes
        if spec['reason']:
            return spec['reason']
        source = nodes.get(spec['source'])
        if not source or source['deleting'] or source['desired_mode'] != 'active' or source['installed_agent'] != 'active' or source['manual_required']:
            return 'unknown', '发起端不是已确认的主动节点'
        if not source['quality_port']:
            return 'disabled', '节点已关闭质量探测'
        caps = json.loads(source['capabilities'])
        cap = {'tcp-connect-v1': 'network_quality_tcp', 'icmp-echo-v1': 'network_quality_icmp', 'udp-echo-v1': 'network_quality'}[spec['protocol']]
        if cap not in caps:
            return 'unsupported', '请升级发起端 Agent'
        session = self.hub.sessions.get(source['id'])
        if not session or session.get('paused') or not session.get('quality_capable'):
            return 'offline', '主动 Agent 未连接'
        if spec['target']:
            pair = dict(source=spec['source'], target=spec['target'], protocol=spec['protocol'], port=spec['port'])
            return self.parent.unavailable(nodes, pair)
        for nid in spec['route']:
            n = nodes.get(nid)
            if n and (n['manual_required'] or n['applied_generation'] != n['desired_generation']):
                return 'waiting', '等待路径配置确认'
        return None

    async def prepare(self, now):
        def read(c):
            nodes = {r['id']: dict(r) for r in c.execute('SELECT * FROM nodes')}
            specs = [p for l in c.execute("SELECT * FROM links WHERE state NOT IN ('draft','deleting')")
                     for section in sections(self.hub.s, c, l, nodes) for p in section['probes']]
            if now >= self.parent.cleanup_at:
                c.execute('DELETE FROM link_quality_samples WHERE slot<?', (now - 86400,))
            return nodes, specs
        self.nodes, specs = await self.hub.database(read)
        self.plans = {self.key(p): p for p in specs}
        self.last = {k: v for k, v in self.last.items() if k in self.plans}
        needed = set()
        for spec in specs:
            if spec['protocol'] == 'udp-echo-v1' and not spec['reason'] and spec['target']:
                pair = dict(source=spec['source'], target=spec['target'], protocol=spec['protocol'])
                # Listener readiness is handled by the shared scheduler.
                import network_quality as q
                if not q.stopped(self.nodes, pair):
                    needed.update((spec['source'], spec['target']))
        return needed

    async def view(self, body, now):
        import network_quality as q
        def read(c):
            link = pools.get(self.hub.s, c, 'links', body['id'])
            nodes = {r['id']: dict(r) for r in c.execute('SELECT * FROM nodes')}
            result = []
            for section in sections(self.hub.s, c, link, nodes):
                series = []
                for spec in section.pop('probes'):
                    rows = [dict(r) for r in c.execute('SELECT * FROM link_quality_samples WHERE link_id=? AND section=? AND protocol=? AND slot>=? ORDER BY slot',
                            (*self.key(spec), now - max(body.get('range', 900), 300) - 300))]
                    reason = self.reason(spec, nodes)
                    history = q.summarize(rows, body.get('range', 900), now)
                    history['summary'] = q.aggregate([r for r in rows if r['slot'] >= now - 300 and r['path_key'] == spec['path_key']])
                    series.append(dict(protocol=spec['protocol'], state=reason[0] if reason else 'collecting',
                                       note=reason[1] if reason else '', path_key=spec['path_key'],
                                       **history))
                result.append(dict(section, series=series))
            return dict(name=link['name'], sections=result, now=now, interval=q.INTERVAL)
        return await self.hub.database(read)

    async def tick(self, now):
        import network_quality as q
        for op, task in list(self.pending.items()):
            if (any(self.plans.get(self.key(p), {}).get('path_key') != p['path_key'] for p in task['specs'])
                    or task['session'] is not self.hub.sessions.get(task['specs'][0]['source'])):
                self.pending.pop(op, None)
            elif now - task['at'] >= 8:
                self.pending.pop(op, None)
                await self.hub.database(lambda c, t=task: [record(c, p, t['at'], 'error', error='探测结果未返回') for p in t['specs']])
        busy = {self.key(p) for t in self.pending.values() for p in t['specs']}
        errors, groups = [], {}
        for key, spec in sorted(self.plans.items(), key=lambda kv: self.last.get(kv[0], 0)):
            if key in busy or now - self.last.get(key, 0) < q.INTERVAL:
                continue
            reason = self.reason(spec)
            if reason:
                self.last[key] = now
                errors.append((spec, reason))
            else:
                groups.setdefault(spec['path_key'], []).append(spec)
        if errors:
            await self.hub.database(lambda c: [record(c, p, now, r[0], error=r[1]) for p, r in errors])
        for specs in groups.values():
            spec = specs[0]
            touched = {n for n in spec['route'] if n}
            if any(self.parent.budget.get(n) == now for n in touched):
                continue
            pending_count = sum(t['specs'][0]['source'] == spec['source'] for t in self.pending.values())
            pending_count += sum(t['pair']['source'] == spec['source'] for t in self.parent.pending.values())
            if pending_count >= 2:
                continue
            for n in touched:
                self.parent.budget[n] = now
            for p in specs:
                self.last[self.key(p)] = now
            session = self.hub.sessions[spec['source']]
            target = self.hub.sessions.get(spec['target'])
            op = secrets.token_hex(16)
            self.pending[op] = dict(specs=specs, at=now, session=session, target_session=target)
            message = dict(type='quality_probe', id=op, protocol=spec['protocol'], host=spec['host'], port=spec['port'],
                           key=target.get('quality_key', '') if target else '')
            if spec['rule_id']:
                message['rule_id'] = spec['rule_id']
            try:
                await asyncio.wait_for(session['send'](message), 2)
            except (ConnectionError, asyncio.TimeoutError):
                self.pending.pop(op, None)
                await self.hub.database(lambda c: [record(c, p, now, 'error', error='探测指令发送失败') for p in specs])

    async def result(self, nid, session, data):
        task = self.pending.get(data.get('id'))
        if not task or task['session'] is not session or task['specs'][0]['source'] != nid or self.hub.sessions.get(nid) is not session:
            return
        self.pending.pop(data['id'], None)
        status, rtt = data.get('status'), data.get('rtt')
        if status not in ('ok', 'timeout', 'error') or (status == 'ok' and (type(rtt) not in (int, float) or not math.isfinite(rtt) or not 0 <= rtt <= 4000)):
            return
        peer = task['specs'][0]['target']
        if peer and task['target_session'] is not self.hub.sessions.get(peer):
            return
        def save(c):
            nodes = {r['id']: dict(r) for r in c.execute('SELECT * FROM nodes')}
            for spec in task['specs']:
                link = c.execute('SELECT * FROM links WHERE id=?', (spec['link_id'],)).fetchone()
                if not link:
                    continue
                current = next((p for sec in sections(self.hub.s, c, link, nodes) for p in sec['probes'] if self.key(p) == self.key(spec)), None)
                if current and current['path_key'] == spec['path_key'] and not self.reason(current, nodes):
                    record(c, spec, task['at'], status, rtt if status == 'ok' else None, data.get('error', ''), data.get('resolved_ip', ''))
        await self.hub.database(save)
