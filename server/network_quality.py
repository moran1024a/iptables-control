"""Node-pair UDP and ICMP measurements. Routes choose pairs; measurements do not own routes."""
import asyncio
from collections import defaultdict
import json
import logging
import math
import secrets
import time
import pools
import link_quality

INTERVAL = 10
PORT = 33445
PROTOCOLS = ('udp-echo-v1', 'icmp-echo-v1')
SCHEMA = """
CREATE TABLE IF NOT EXISTS quality_pairs (
 id INTEGER PRIMARY KEY, source INTEGER NOT NULL, target INTEGER NOT NULL,
 source_addr TEXT NOT NULL, target_addr TEXT NOT NULL, port INTEGER NOT NULL,
 protocol TEXT NOT NULL DEFAULT 'udp-echo-v1',
 UNIQUE(source,target,source_addr,target_addr,port,protocol)
);
CREATE TABLE IF NOT EXISTS quality_samples (
 pair_id INTEGER NOT NULL, slot INTEGER NOT NULL, status TEXT NOT NULL,
 rtt REAL, error TEXT NOT NULL DEFAULT '', resolved_ip TEXT NOT NULL DEFAULT '',
 PRIMARY KEY(pair_id,slot)
);
CREATE INDEX IF NOT EXISTS quality_sample_age ON quality_samples(slot);
"""


def migrate(s, conn):
    if conn.execute('PRAGMA user_version').fetchone()[0] >= 401:
        return
    s.backup_database(conn, s.DB_PATH + '.pre-0.4.0-rc.2.bak')
    with conn:
        conn.execute('BEGIN IMMEDIATE')
        conn.execute(f'ALTER TABLE nodes ADD COLUMN quality_port INTEGER NOT NULL DEFAULT {PORT}')
        for stmt in SCHEMA.split(';'):
            if stmt.strip():
                conn.execute(stmt)
        conn.execute('PRAGMA user_version=401')


def paths(conn):
    result = {('routes', r['id']): json.loads(r['node_ids']) for r in conn.execute('SELECT * FROM routes')}
    for c in conn.execute('SELECT id FROM chains WHERE deleting=0'):
        result[('configs', c['id'])] = [h[0] for h in conn.execute(
            'SELECT node_id FROM hops WHERE chain_id=? ORDER BY seq', (c['id'],))]
    result.update(pools.quality_paths(conn)[0])
    return result


def ensure_pair(conn, nodes, a, b, protocol='udp-echo-v1'):
    key = (a, b, nodes[a]['addr'], nodes[b]['addr'], nodes[b]['quality_port'] if protocol == 'udp-echo-v1' else 0, protocol)
    conn.execute('INSERT OR IGNORE INTO quality_pairs(source,target,source_addr,target_addr,port,protocol) VALUES (?,?,?,?,?,?)', key)
    return dict(conn.execute('SELECT * FROM quality_pairs WHERE source=? AND target=? AND source_addr=? AND target_addr=? AND port=? AND protocol=?', key).fetchone())


def stopped(nodes, pair):
    for nid in (pair['source'], pair['target']):
        n = nodes.get(nid)
        if not n or n['deleting']:
            return 'stopped', '节点已删除或正在删除'
        if n['desired_mode'] != 'active' or n['installed_agent'] == 'passive' or n['manual_required']:
            return 'stopped', '仅已确认的主动节点参与探测'
        if n['installed_agent'] != 'active' or 'network_quality' not in json.loads(n['capabilities']):
            return 'unsupported', '请将两端主动 Agent 升级到 0.4.0'
        if not n['quality_port']:
            return 'disabled', '节点已关闭质量探测'
    if pair['protocol'] == 'icmp-echo-v1' and 'network_quality_icmp' not in json.loads(nodes[pair['source']]['capabilities']):
        return 'unsupported', '请升级发起节点的主动 Agent 以支持 ICMP'
    return None


def record(conn, pair_id, slot, status, rtt=None, error='', resolved_ip=''):
    conn.execute('INSERT OR IGNORE INTO quality_samples VALUES (?,?,?,?,?,?)',
                 (pair_id, slot, status, rtt, str(error)[:300], str(resolved_ip)[:64]))


def aggregate(rows):
    good = [r['rtt'] for r in rows if r['status'] == 'ok' and r['rtt'] is not None]
    lost = sum(r['status'] == 'timeout' for r in rows)
    sent = len(good) + lost
    changes = [abs(b['rtt'] - a['rtt']) for a, b in zip(rows, rows[1:])
               if a['status'] == b['status'] == 'ok' and b['slot'] - a['slot'] <= INTERVAL * 2
               and a['resolved_ip'] == b['resolved_ip'] and a.get('path_key') == b.get('path_key')]
    ordered = sorted(good)
    return dict(samples=sent, lost=lost, loss=lost * 100 / sent if sent else None,
                mean=sum(good) / len(good) if good else None, peak=max(good) if good else None,
                p95=ordered[math.ceil(len(ordered) * .95) - 1] if ordered else None,
                jitter=sum(changes) / len(changes) if changes else None)


def history(conn, pair_id, seconds, now):
    rows = [dict(r) for r in conn.execute('SELECT * FROM quality_samples WHERE pair_id=? AND slot>=? ORDER BY slot',
                                         (pair_id, now - max(seconds, 300) - 300))]
    return summarize(rows, seconds, now)


def summarize(rows, seconds, now):
    step = {900: 10, 3600: 60, 86400: 300}[seconds]
    buckets = defaultdict(list)
    for row in rows:
        buckets[row['slot'] // step * step].append(row)
    points = []
    for slot in range((now - seconds) // step * step, now // step * step + 1, step):
        batch = buckets.get(slot, [])
        stat = aggregate(batch)
        points.append(dict(time=slot, **stat, status=batch[-1]['status'] if batch else 'missing',
                           error=batch[-1]['error'] if batch else '',
                           resolved_ip=batch[-1]['resolved_ip'] if batch else '',
                           gap=any(r['status'] != 'ok' for r in batch) or len(batch) < step // INTERVAL,
                           path_key=batch[-1].get('path_key') if batch else None,
                           path_label=batch[-1].get('path_label', '') if batch else '',
                           endpoint_change=len({(r['resolved_ip'], r.get('path_key')) for r in batch}) > 1))
    recent = [r for r in rows if r['slot'] >= now - 300]
    latest = rows[-1] if rows else None
    return dict(points=points, step=step, summary=aggregate(recent), latest=latest)



class Monitor:
    def __init__(self, hub):
        self.hub = hub
        self.extra = {}  # Short leases for nonadjacent pairs currently viewed in the panel.
        self.pending = {}
        self.last = {}
        self.budget = {}
        self.task = None
        self.cleanup_at = 0
        self.links = link_quality.Monitor(self)

    def unavailable(self, nodes, pair):
        reason = stopped(nodes, pair)
        if reason:
            return reason
        for nid in (pair['source'], pair['target']):
            session = self.hub.sessions.get(nid)
            if not session or session.get('paused') or not session.get('quality_capable'):
                return 'offline', '主动 Agent 未连接'
        if pair['protocol'] == 'icmp-echo-v1':
            return None
        target = self.hub.sessions.get(pair['target'], {})
        q = target.get('quality_listener', {})
        if q.get('port') != pair['port'] or q.get('id') != target.get('quality_setup_id'):
            return 'waiting', '等待探测端口就绪'
        if q.get('error'):
            return 'error', q['error']
        return None

    async def view(self, body):
        scope, ident = body.get('scope'), body.get('id')
        seconds = body.get('range', 900)
        if scope not in ('routes', 'configs', 'links') or type(ident) is not int or seconds not in (900, 3600, 86400):
            raise self.hub.s.UserError('无效的链路或时间范围')
        now = int(time.time())
        if scope == 'links' and 'source' not in body and 'target' not in body:
            return await self.links.view(body, now)

        def read(conn):
            ids = paths(conn).get((scope, ident))
            if ids is None:
                raise self.hub.s.UserError('链路不存在', status=404)
            nodes = {r['id']: dict(r) for r in conn.execute('SELECT * FROM nodes')}
            ids = list(dict.fromkeys(ids))
            options = [dict(id=n, name=nodes[n]['name'], mode=nodes[n]['desired_mode']) for n in ids]
            if len(ids) < 2:
                return dict(nodes=options, pair=None, now=now)
            a, b = body.get('source', ids[0]), body.get('target', ids[1])
            if type(a) is not int or type(b) is not int or a not in ids or b not in ids or a == b:
                raise self.hub.s.UserError('请选择当前链路内的两个不同节点')
            series = []
            for protocol in PROTOCOLS:
                pair = ensure_pair(conn, nodes, a, b, protocol)
                why = self.unavailable(nodes, pair)
                series.append(dict(protocol=protocol, pair=pair, state=why[0] if why else 'collecting',
                                   note=why[1] if why else '', **history(conn, pair['id'], seconds, now)))
            return dict(nodes=options, pair=series[0]['pair'], now=now, interval=INTERVAL,
                        adjacent=(a, b) in (pools.quality_paths(conn, ident)[1] if scope == 'links' else set(zip(ids, ids[1:]))), series=series)
        result = await self.hub.database(read)
        if result['pair']:
            p = result['pair']
            self.extra[(scope, ident, p['source'], p['target'])] = now + 60
        return result

    async def tick(self):
        now = int(time.time())
        self.extra = {k: v for k, v in self.extra.items() if v > now}

        link_needed = await self.links.prepare(now)
        extra = tuple(self.extra)

        def plan(conn):
            routes = paths(conn)
            nodes = {r['id']: dict(r) for r in conn.execute('SELECT * FROM nodes')}
            linked = {r[0] for r in conn.execute('SELECT chain_id FROM links WHERE chain_id IS NOT NULL')}
            edges = {edge for (scope, ident), ids in routes.items() if scope == 'routes' or (scope == 'configs' and ident not in linked)
                     for edge in zip(ids, ids[1:]) if edge[0] != edge[1]}
            for scope, ident, a, b in extra:
                ids = routes.get((scope, ident), [])
                if a in ids and b in ids:
                    edges.add((a, b))
            pairs = [ensure_pair(conn, nodes, a, b, protocol) for a, b in sorted(edges)
                     if a in nodes and b in nodes for protocol in PROTOCOLS]
            if now >= self.cleanup_at:
                conn.execute('DELETE FROM quality_samples WHERE slot<?', (now - 86400,))
                conn.execute('DELETE FROM quality_pairs WHERE id NOT IN (SELECT DISTINCT pair_id FROM quality_samples) '
                             'AND (source NOT IN (SELECT id FROM nodes) OR target NOT IN (SELECT id FROM nodes))')
            return nodes, pairs
        nodes, pairs = await self.hub.database(plan)
        if now >= self.cleanup_at:
            self.cleanup_at = now + 60
        active_ids = {p['id'] for p in pairs}
        self.last = {k: v for k, v in self.last.items() if k in active_ids}
        needed = {n for p in pairs if p['protocol'] == 'udp-echo-v1' and not stopped(nodes, p) for n in (p['source'], p['target'])}
        needed.update(link_needed)
        for nid, session in list(self.hub.sessions.items()):
            if not session.get('quality_capable') or session.get('paused'):
                continue
            port = nodes[nid]['quality_port'] if nid in needed else 0
            if (session.get('quality_port') != port or
                    ((not session.get('quality_listener') or session['quality_listener'].get('error'))
                     and now - session.get('quality_setup_at', 0) >= 30)):
                session['quality_setup_at'] = now
                session['quality_port'] = port
                session['quality_setup_id'] = secrets.token_hex(16)
                session['quality_key'] = secrets.token_hex(32)
                session.pop('quality_listener', None)
                await asyncio.wait_for(session['send'](dict(type='quality_setup', id=session['quality_setup_id'], port=port,
                                                            key=session['quality_key'])), 2)
        for op, task in list(self.pending.items()):
            if (task['pair']['id'] not in active_ids or stopped(nodes, task['pair'])
                    or task['session'] is not self.hub.sessions.get(task['pair']['source'])
                    or task['target_session'] is not self.hub.sessions.get(task['pair']['target'])):
                self.pending.pop(op, None)
            elif now - task['at'] >= 8:
                self.pending.pop(op, None)
                await self.hub.database(lambda c: record(c, task['pair']['id'], task['at'], 'error', error='探测结果未返回'))
        busy = {x['pair']['id'] for x in self.pending.values()}
        self.budget = {n: t for n, t in self.budget.items() if t >= now - 1}
        for pair in sorted(pairs, key=lambda p: self.last.get(p['id'], 0)):
            pid = pair['id']
            if pid in busy or now - self.last.get(pid, 0) < INTERVAL:
                continue
            why = self.unavailable(nodes, pair)
            if why:
                self.last[pid] = now
                await self.hub.database(lambda c: record(c, pid, now, why[0], error=why[1]))
                continue
            a, b = pair['source'], pair['target']
            if any(self.budget.get(n) == now for n in (a, b)) or sum(x['pair']['source'] == a for x in self.pending.values()) >= 2:
                continue
            self.budget[a] = self.budget[b] = now  # At most one scheduled packet per node per second.
            self.last[pid] = now
            source, target = self.hub.sessions[a], self.hub.sessions[b]
            op = secrets.token_hex(16)
            self.pending[op] = dict(pair=pair, at=now, session=source, target_session=target)
            await asyncio.wait_for(source['send'](dict(type='quality_probe', id=op, host=pair['target_addr'],
                                                       port=pair['port'], protocol=pair['protocol'], key=target.get('quality_key', ''))), 2)

        await self.links.tick(now)

    async def result(self, nid, session, data):
        if data.get('id') in self.links.pending:
            return await self.links.result(nid, session, data)
        task = self.pending.get(data.get('id'))
        if not task or task['session'] is not session or task['pair']['source'] != nid:
            return
        self.pending.pop(data['id'], None)
        if task['target_session'] is not self.hub.sessions.get(task['pair']['target']):
            return
        status, rtt = data.get('status'), data.get('rtt')
        if status not in ('ok', 'timeout', 'error'):
            return
        if status == 'ok' and (type(rtt) not in (int, float) or not math.isfinite(rtt) or not 0 <= rtt <= 4000):
            return
        def save(conn):
            nodes = {r['id']: dict(r) for r in conn.execute('SELECT * FROM nodes')}
            p = task['pair']
            if (stopped(nodes, p) or nodes[p['source']]['addr'] != p['source_addr']
                    or nodes[p['target']]['addr'] != p['target_addr']
                    or (p['protocol'] == 'udp-echo-v1' and nodes[p['target']]['quality_port'] != p['port'])):
                return
            record(conn, p['id'], task['at'], status, rtt if status == 'ok' else None,
                   data.get('error', ''), data.get('resolved_ip', ''))
        await self.hub.database(save)

    async def run(self):
        while True:
            try:
                await self.tick()
            except (ConnectionError, asyncio.TimeoutError):
                pass
            except Exception:
                logging.exception('network quality scheduler')
            await asyncio.sleep(1)
