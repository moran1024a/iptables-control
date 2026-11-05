"""Pool templates, independent link instances and receipt-driven failover.

The scheduler compiles ordinary forwarding rules. Agents never choose a route.
Prepared rules use separate internal ports; the stable entry is switched last.
"""
import asyncio
import json
import logging
import secrets
import time

OFFLINE_AFTER = 60
WAIT_SECONDS = 30


def dump(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'))


def default_member(stage):
    # Older plans had no separate default; their first priority was the default.
    return stage.get('default', next(iter(stage['order']), None))


def migrate(s, c):
    if c.execute('PRAGMA user_version').fetchone()[0] >= 403:
        return
    s.backup_database(c, s.DB_PATH + '.pre-0.4.0-rc.4.bak')
    with c:
        c.execute('BEGIN IMMEDIATE')
        c.execute("CREATE TABLE IF NOT EXISTS pools(id INTEGER PRIMARY KEY, name TEXT NOT NULL, members TEXT NOT NULL DEFAULT '[]')")
        c.execute("""CREATE TABLE IF NOT EXISTS links(
            id INTEGER PRIMARY KEY, name TEXT NOT NULL, chain_id INTEGER UNIQUE REFERENCES chains(id) ON DELETE SET NULL,
            plan TEXT NOT NULL, selected TEXT NOT NULL DEFAULT '{}', managed INTEGER NOT NULL DEFAULT 0,
            state TEXT NOT NULL DEFAULT 'draft', error TEXT NOT NULL DEFAULT '', job TEXT,
            failures TEXT NOT NULL DEFAULT '[]', retained TEXT NOT NULL DEFAULT '[]', revision INTEGER NOT NULL DEFAULT 1)""")
        c.execute("""CREATE TABLE IF NOT EXISTS link_rules(
            id INTEGER PRIMARY KEY, link_id INTEGER NOT NULL REFERENCES links(id) ON DELETE CASCADE,
            phase TEXT NOT NULL, node_id INTEGER NOT NULL REFERENCES nodes(id), port INTEGER NOT NULL,
            host TEXT NOT NULL, tport INTEGER NOT NULL, UNIQUE(node_id,port))""")
        c.execute("""CREATE TABLE IF NOT EXISTS link_retired(
            link_id INTEGER NOT NULL REFERENCES links(id) ON DELETE CASCADE,
            node_id INTEGER NOT NULL, port INTEGER NOT NULL, PRIMARY KEY(link_id,node_id,port))""")
        c.execute("""CREATE TABLE IF NOT EXISTS link_events(
            id INTEGER PRIMARY KEY, link_id INTEGER NOT NULL REFERENCES links(id) ON DELETE CASCADE,
            ts INTEGER NOT NULL, message TEXT NOT NULL)""")
        for row in c.execute('SELECT * FROM chains').fetchall():
            hops = list(c.execute('SELECT * FROM hops WHERE chain_id=? ORDER BY seq', (row['id'],)))
            plan = fixed_plan(hops, row['target_id'])
            c.execute('INSERT INTO links(name,chain_id,plan,state) VALUES (?,?,?,?)',
                      (row['remark'], row['id'], dump(plan), 'deleting' if row['deleting'] else 'active'))
        for row in c.execute('SELECT * FROM routes WHERE id NOT IN (SELECT route_id FROM chains WHERE route_id IS NOT NULL)').fetchall():
            ids = json.loads(row['node_ids'])
            c.execute('INSERT INTO links(name,plan) VALUES (?,?)',
                      (row['name'], dump(fixed_plan([dict(node_id=n, port=None) for n in ids], None))))
        # The new UI owns complete links. Do not leave invisible legacy templates
        # retaining node references after their forwarding configurations are removed.
        c.execute('UPDATE chains SET route_id=NULL')
        c.execute('DELETE FROM routes')
        c.execute('PRAGMA user_version=403')


def fixed_plan(hops, target):
    return dict(entry=hops[0]['node_id'] if hops else None,
                port=hops[0]['port'] if hops else None, target=target,
                stages=[dict(id=secrets.token_hex(6), kind='node', node_id=h['node_id']) for h in hops[1:]])


def sync_legacy(c):
    """Keep legacy API-created configurations visible without changing their wire rules."""
    for chain in c.execute('SELECT * FROM chains').fetchall():
        link = c.execute('SELECT * FROM links WHERE chain_id=?', (chain['id'],)).fetchone()
        if link and link['managed']:
            continue
        hops = list(c.execute('SELECT * FROM hops WHERE chain_id=? ORDER BY seq', (chain['id'],)))
        plan = fixed_plan(hops, chain['target_id'])
        if link:
            old = json.loads(link['plan'])
            for a, b in zip(plan['stages'], old['stages']):
                if a.get('node_id') == b.get('node_id'): a['id'] = b['id']
            c.execute('UPDATE links SET name=?,plan=?,state=? WHERE id=?',
                      (chain['remark'], dump(plan), 'deleting' if chain['deleting'] else 'active', link['id']))
        else:
            c.execute("INSERT INTO links(name,chain_id,plan,state) VALUES (?,?,?,'active')", (chain['remark'], chain['id'], dump(plan)))


def refresh_links(s, c, node=None, target=None):
    for row in c.execute("SELECT * FROM links WHERE managed=1 AND state!='deleting'").fetchall():
        link = dict(row); plan = json.loads(link['plan'])
        path = [r[0] for r in c.execute('SELECT node_id FROM hops WHERE chain_id=? ORDER BY seq', (link['chain_id'],))]
        if (node is not None and node in path) or (target is not None and plan['target'] == target):
            begin(s, c, link, plan, json.loads(link['selected']), path, '资源地址更新')


def get(s, c, table, ident):
    if type(ident) is not int or ident <= 0:
        raise s.UserError('无效的对象 ID')
    row = c.execute(f'SELECT * FROM {table} WHERE id=?', (ident,)).fetchone()
    if row is None:
        raise s.UserError('对象不存在', status=404)
    return dict(row)


def event(c, lid, message):
    c.execute('INSERT INTO link_events(link_id,ts,message) VALUES (?,?,?)', (lid, int(time.time()), message))
    c.execute('DELETE FROM link_events WHERE link_id=? AND id NOT IN (SELECT id FROM link_events WHERE link_id=? ORDER BY id DESC LIMIT 100)', (lid, lid))


def references(c, pool_id):
    result = []
    for l in c.execute('SELECT * FROM links'):
        stages = json.loads(l['plan'])['stages'] + [dict(x, retained=True) for x in json.loads(l['retained'])]
        result.extend((dict(l), stage) for stage in stages if stage['kind'] == 'pool' and stage['pool_id'] == pool_id)
    return result


def protected(c, link, stage, node):
    selected = json.loads(link['selected'])
    if stage.get('locked') == node or selected.get(stage['id']) == node:
        return True
    if link['job'] and node in json.loads(link['job']).get('nodes', []):
        return True
    return bool(c.execute('SELECT 1 FROM link_retired WHERE link_id=? AND node_id=?', (link['id'], node)).fetchone())


def node_references(c, nid):
    names = ['池：' + p['name'] for p in c.execute('SELECT * FROM pools') if nid in json.loads(p['members'])]
    for link in c.execute('SELECT * FROM links'):
        plan = json.loads(link['plan'])
        if plan['entry'] == nid or any(x['kind'] == 'node' and x['node_id'] == nid for x in plan['stages']):
            names.append('链路：' + link['name'])
    if c.execute('SELECT 1 FROM link_retired WHERE node_id=?', (nid,)).fetchone():
        names.append('等待旧规则清理')
    return names


def reserved(c, nid):
    result = {r[0] for r in c.execute('SELECT port FROM link_rules WHERE node_id=? UNION SELECT port FROM link_retired WHERE node_id=?', (nid, nid))}
    for row in c.execute('SELECT job FROM links WHERE job IS NOT NULL'):
        result.update(r['port'] for r in json.loads(row['job'])['new'] if r['node_id'] == nid)
    return result


def pool_save(s, c, body, ident=None):
    old = get(s, c, 'pools', ident) if ident else None
    name = s.check_remark(body.get('name', old['name'] if old else ''), 'name', '名称')
    members = body.get('members', json.loads(old['members']) if old else [])
    if not isinstance(members, list) or len(members) > 32 or any(type(n) is not int for n in members) or len(set(members)) != len(members):
        raise s.UserError('池最多包含 32 个不重复的主动节点')
    for nid in members:
        n = s.get_node(c, nid)
        if n['deleting'] or n['desired_mode'] != 'active' or n['installed_agent'] != 'active' or n['manual_required']:
            raise s.UserError(f"{n['name']} 尚不是已确认的主动节点")
    if not old:
        ident = c.execute('INSERT INTO pools(name,members) VALUES (?,?)', (name, dump(members))).lastrowid
    else:
        before = json.loads(old['members'])
        removed, added = set(before) - set(members), [n for n in members if n not in before]
        affected = {}
        for link, stage in references(c, ident):
            if stage.get('retained'):
                if removed.intersection(stage['order']):
                    raise s.UserError(f"链路「{link['name']}」仍在使用或清理此池，请等待完成", status=409)
                continue
            for nid in removed:
                if protected(c, link, stage, nid):
                    raise s.UserError(f"成员被链路「{link['name']}」使用或锁定，请先切换并等待清理完成", status=409)
            if link['job'] and (removed or added):
                raise s.UserError(f"链路「{link['name']}」正在切换，请稍后修改成员", status=409)
            plan = affected.setdefault(link['id'], json.loads(link['plan']))
            instance = next(x for x in plan['stages'] if x['id'] == stage['id'])
            instance['default'] = default_member(instance)
            instance['order'] = [n for n in instance['order'] if n not in removed] + added
            instance['disabled'] = [n for n in instance['disabled'] if n not in removed]
            if instance['default'] not in instance['order']:
                instance['default'] = next(iter(instance['order']), None)
        for lid, plan in affected.items():
            c.execute('UPDATE links SET plan=?, revision=revision+1 WHERE id=?', (dump(plan), lid))
        c.execute('UPDATE pools SET name=?,members=? WHERE id=?', (name, dump(members), ident))
    return dict(id=ident)


def pool_delete(s, c, ident):
    get(s, c, 'pools', ident)
    refs = references(c, ident)
    if refs:
        raise s.UserError('池仍被链路引用：' + '、'.join(dict.fromkeys(l['name'] for l, _ in refs)), status=409)
    c.execute('DELETE FROM pools WHERE id=?', (ident,))
    return dict(deleted=True)


def validate_plan(s, c, raw, old=None):
    if not isinstance(raw, dict) or not isinstance(raw.get('stages', []), list) or len(raw.get('stages', [])) > 30:
        raise s.UserError('链路最多包含 30 个中间阶段')
    entry, target = raw.get('entry'), raw.get('target')
    if entry is not None:
        n = s.get_node(c, entry)
        if n['deleting']:
            raise s.UserError('入口正在删除')
    if target is not None:
        s.get_target(c, target)
    port = raw.get('port')
    port = s.check_port(port, 'port') if port not in (None, '') else None
    result = dict(entry=entry, port=port, target=target, stages=[])
    old_stages = {x['id']: x for x in json.loads(old['plan'])['stages']} if old else {}
    seen = set()
    for value in raw.get('stages', []):
        if not isinstance(value, dict):
            raise s.UserError('无效的链路阶段')
        sid = value.get('id') or secrets.token_hex(6)
        if not isinstance(sid, str) or not 1 <= len(sid) <= 64 or sid in seen:
            raise s.UserError('阶段标识必须唯一')
        seen.add(sid)
        kind = value.get('kind')
        if kind == 'node':
            n = s.get_node(c, value.get('node_id'))
            if n['deleting']:
                raise s.UserError('节点正在删除')
            result['stages'].append(dict(id=sid, kind=kind, node_id=n['id']))
            continue
        if kind != 'pool':
            raise s.UserError('阶段只能是固定节点或池')
        pool = get(s, c, 'pools', value.get('pool_id'))
        members = json.loads(pool['members'])
        previous = old_stages.get(sid)
        if previous and (previous['kind'] != 'pool' or previous['pool_id'] != pool['id']):
            previous = None
        direct = value.get('direct', previous.get('direct', False) if previous else False)
        if type(direct) is not bool:
            raise s.UserError('直连开关必须是布尔值')
        available = members + ([0] if direct else [])
        order = value.get('order')
        order = order if order is not None else (previous['order'] if previous else members)
        if not isinstance(order, list) or any(type(n) is not int for n in order) or len(set(order)) != len(order):
            raise s.UserError('成员优先级无效')
        order = [n for n in order if n in available] + [n for n in available if n not in order]
        default = value.get('default', default_member(previous) if previous else next(iter(order), None))
        if default is None and order:
            default = order[0]
        if default is not None and (type(default) is not int or default not in order):
            raise s.UserError('默认节点必须是池中的成员')
        disabled = value.get('disabled', previous['disabled'] if previous else [])
        locked = value.get('locked', previous.get('locked') if previous else None)
        if not isinstance(disabled, list) or any(type(n) is not int or n not in available for n in disabled):
            raise s.UserError('禁用成员无效')
        if locked is not None and (type(locked) is not int or locked not in available or locked in disabled):
            raise s.UserError('锁定成员必须存在且未禁用')
        if old and previous:
            current = json.loads(old['selected']).get(sid)
            if current == 0 and not direct:
                raise s.UserError('请先切换到其他成员，再移除正在使用的直连规则', status=409)
            if current is not None and current in disabled and current not in previous['disabled']:
                raise s.UserError('请先切换当前成员，再将它禁用', status=409)
        result['stages'].append(dict(id=sid, kind=kind, pool_id=pool['id'], order=order,
                                     disabled=disabled, locked=locked, direct=direct, default=default))
    if entry is None and result['stages']:
        raise s.UserError('添加中间阶段之前请选择入口')
    return result


def ready(n, now):
    return (not n['deleting'] and n['desired_mode'] == 'active' and n['installed_agent'] == 'active'
            and not n['manual_required'] and n['last_seen'] is not None and now - n['last_seen'] < 30
            and n['state'] == 'ok' and n['applied_generation'] == n['desired_generation'])


def choose(s, c, link, plan, prefer_current=False, forced=None, now=None):
    now = int(time.time()) if now is None else now
    nodes = {n['id']: n for n in c.execute('SELECT * FROM nodes')}
    current, failed = json.loads(link['selected']), json.loads(link['failures'])
    options = []
    for stage in plan['stages']:
        if stage['kind'] == 'node':
            options.append((stage['id'], [stage['node_id']]))
            continue
        sid = stage['id']
        order = list(stage['order'])
        preferred = current[sid] if prefer_current and sid in current else default_member(stage)
        if preferred in order:
            order.remove(preferred); order.insert(0, preferred)
        if stage['locked'] is not None:
            order = [stage['locked']]
        if forced and sid in forced:
            if stage['locked'] is not None and stage['locked'] != forced[sid]:
                raise s.UserError('请先解除成员锁定')
            order = [forced[sid]] if forced[sid] in order else []
        order = [n for n in order if n not in stage['disabled'] and n not in failed
                 and (n == 0 or (n in nodes and ready(nodes[n], now)))]
        if not order:
            raise s.UserError('池中没有可用成员，请检查在线状态、锁定和禁用设置')
        options.append((sid, order))
    # Prefer lexicographic manual priorities; reject loops, never enumerate paths for storage.
    visits = 0
    def walk(index, used, picked, path):
        nonlocal visits
        visits += 1
        if visits > 4096:
            raise s.UserError('候选组合过多，请减少重复节点或拆分链路')
        if index == len(options):
            return picked, path
        sid, values = options[index]
        for nid in values:
            if nid and nid in used:
                continue
            answer = walk(index + 1, used | ({nid} if nid else set()), {**picked, sid: nid}, path + ([nid] if nid else []))
            if answer:
                return answer
    answer = walk(0, {plan['entry']} if plan['entry'] else set(), {}, [plan['entry']] if plan['entry'] else [])
    if not answer:
        raise s.UserError('候选路径形成节点循环，请调整链路')
    return answer


def next_rule(s, c):
    value = int(s.setting(c, 'next_hop_id'))
    s.set_setting(c, 'next_hop_id', str(value + 1))
    return value


def rows(c, lid):
    return [dict(r) for r in c.execute("SELECT * FROM link_rules WHERE link_id=? AND phase='live' ORDER BY id", (lid,))]


def begin(s, c, link, plan, selected, path, reason):
    if link['job']:
        raise s.UserError('链路已有未完成的切换，请先等待确认或重试同步', status=409)
    if plan['target'] is None:
        raise s.UserError('请选择目标')
    target = s.get_target(c, plan['target'])
    cid = link['chain_id']
    if cid is None:
        cid = c.execute('INSERT INTO chains(remark,target_id,group_id) VALUES (?,?,?)',
                        (link['name'], target['id'], s.default_group(c, 'chains'))).lastrowid
        c.execute('UPDATE links SET chain_id=? WHERE id=?', (cid, link['id']))
    old_hops = [dict(h) for h in c.execute('SELECT * FROM hops WHERE chain_id=? ORDER BY seq', (cid,))]
    if not link['managed']:
        for h in old_hops:
            rule = next(r for r in s.node_rule_rows(c, s.get_node(c, h['node_id'])) if r['id'] == h['id'])
            c.execute("INSERT INTO link_rules VALUES (?,?, 'live',?,?,?,?)",
                      (h['id'], link['id'], h['node_id'], h['port'], rule['host'], rule['tport']))
        c.execute('UPDATE links SET managed=1 WHERE id=?', (link['id'],))
    old = rows(c, link['id'])
    c.execute('UPDATE chains SET route_id=NULL WHERE id=?', (cid,))
    desired_pairs = [(nid, s.get_node(c, path[i+1])['addr'] if i+1 < len(path) else target['host']) for i, nid in enumerate(path)]
    # Metadata/priority edits that keep the same path do not disturb forwarding.
    if ([h['node_id'] for h in old_hops] == path and len(old) == len(path)
            and all(any(r['id'] == h['id'] and r['host'] == host and
                        r['tport'] == (old_hops[i+1]['port'] if i+1 < len(path) else target['port'])
                        for r in old) for i, (h, (_, host)) in enumerate(zip(old_hops, desired_pairs)))
            and (not path or plan['port'] in (None, old_hops[0]['port']))):
        c.execute("UPDATE links SET selected=?,state='active',error='' WHERE id=?", (dump(selected), link['id']))
        c.execute('UPDATE chains SET target_id=?,remark=? WHERE id=?', (target['id'], link['name'], cid))
        return
    new = []
    for index, nid in enumerate(path):
        n = s.get_node(c, nid)
        if n['deleting'] or n['manual_required']:
            raise s.UserError(f"{n['name']} 正在删除或等待模式确认")
        if (any(x['kind'] == 'pool' for x in plan['stages']) and n['desired_mode'] == 'passive'
                and s.agent_v3.newer('0.4.0-rc.4', n['agent'] or '0.0.0')):
            raise s.UserError(f"请先将被动节点 {n['name']} 升级到 0.4.0，以支持切换时清理旧连接")
        existing = old_hops[0] if index == 0 and old_hops and old_hops[0]['node_id'] == nid else None
        port = (plan['port'] or (existing['port'] if existing else None)) if index == 0 else None
        used = reserved(c, nid) | {r[0] for r in c.execute('SELECT port FROM hops WHERE node_id=? UNION SELECT port FROM retired_ports WHERE node_id=?', (nid, nid))}
        used |= {r['port'] for r in new if r['node_id'] == nid}
        if n['desired_mode'] == 'active': used.add(n['quality_port'])
        if n['wake_enabled']: used.add(n['wake_port'])
        ranges = s.parse_ports(n['ports'])
        if port is None:
            port = s.pick_port(ranges, used)
        if port is None or not s.in_ports(port, ranges) or (port in used and not (existing and existing['port'] == port)):
            raise s.UserError(f"节点 {n['name']} 没有可用端口，或入口端口已占用", f'capacity:{nid}')
        new.append(dict(id=next_rule(s, c), node_id=nid, port=port))
    for i, rule in enumerate(new):
        rule.update(host=s.get_node(c, new[i+1]['node_id'])['addr'] if i+1 < len(new) else target['host'],
                    tport=new[i+1]['port'] if i+1 < len(new) else target['port'])
    for r in new[1:]:
        c.execute("INSERT INTO link_rules VALUES (?,?, 'stage',?,?,?,?)", (r['id'], link['id'], r['node_id'], r['port'], r['host'], r['tport']))
    job = dict(phase='prepare', at=int(time.time()), new=new, old=old, selected=selected,
               nodes=sorted({r['node_id'] for r in old + new}), target=target['id'], reason=reason,
               wait=sorted({r['node_id'] for r in new[1:]}))
    c.execute("UPDATE links SET job=?,state='preparing',error='' WHERE id=?", (dump(job), link['id']))
    event(c, link['id'], reason + '：准备新路径')


def link_save(s, c, body, ident=None):
    old = get(s, c, 'links', ident) if ident else None
    if old and body.get('revision') != old['revision']:
        raise s.UserError('链路已变化，请刷新后重试', status=409)
    if old and old['job']:
        raise s.UserError('链路正在切换或等待确认，暂不能编辑', status=409)
    name = s.check_remark(body.get('name', old['name'] if old else ''), 'name', '名称')
    plan = validate_plan(s, c, body.get('plan'), old)
    if old:
        retained = json.loads(old['retained'])
        for st in json.loads(old['plan'])['stages']:
            if st['kind'] == 'pool' and st not in retained:
                retained.append(st)
        c.execute('UPDATE links SET retained=? WHERE id=?', (dump(retained), ident))
        c.execute('UPDATE links SET name=?,plan=?,failures=\'[]\',revision=revision+1 WHERE id=?', (name, dump(plan), ident))
    else:
        ident = c.execute('INSERT INTO links(name,plan) VALUES (?,?)', (name, dump(plan))).lastrowid
    link = get(s, c, 'links', ident)
    if body.get('apply', True):
        old_stages = {x['id']: x for x in json.loads(old['plan'])['stages'] if x['kind'] == 'pool'} if old else {}
        changed_defaults = {x['id']: x['default'] for x in plan['stages'] if x['kind'] == 'pool'
                            and x['id'] in old_stages and x['pool_id'] == old_stages[x['id']]['pool_id']
                            and x['default'] != default_member(old_stages[x['id']])}
        selected, path = choose(s, c, link, plan, prefer_current=bool(old), forced=changed_defaults)
        begin(s, c, link, plan, selected, path, '手动应用')
    elif old and old['chain_id'] is not None:
        raise s.UserError('已经运行的链路请使用保存并应用')
    return dict(id=ident)


def action(s, c, body, ident):
    link = get(s, c, 'links', ident)
    if body.get('revision') != link['revision']:
        raise s.UserError('链路已变化，请刷新后重试', status=409)
    if link['state'] == 'deleting':
        raise s.UserError('链路正在删除，暂不能切换', status=409)
    if body.get('action') == 'retry':
        if not link['job']:
            c.execute("UPDATE links SET failures='[]',error='' WHERE id=?", (ident,))
            plan = json.loads(link['plan'])
            link['failures'] = '[]'
            chosen, path = choose(s, c, link, plan, prefer_current=True)
            begin(s, c, link, plan, chosen, path, '手动重试')
        else:
            job = json.loads(link['job']); job['at'] = int(time.time())
            for nid in job['wait']:
                c.execute('UPDATE nodes SET desired_generation=desired_generation+1 WHERE id=?', (nid,))
                s.wake.queue(s, c, s.get_node(c, nid))
            c.execute("UPDATE links SET job=?,error='',state=? WHERE id=?", (dump(job), 'preparing' if job['phase'] == 'prepare' else 'switching', ident))
            event(c, ident, '手动重试同步')
    elif body.get('action') == 'switch':
        stage_id, member = body.get('stage'), body.get('member')
        plan = json.loads(link['plan'])
        if type(member) is not int or not any(x['kind'] == 'pool' and x['id'] == stage_id for x in plan['stages']):
            raise s.UserError('请选择池中的成员')
        c.execute("UPDATE links SET failures='[]' WHERE id=?", (ident,)); link['failures'] = '[]'
        selected, path = choose(s, c, link, plan, prefer_current=True, forced={stage_id: member})
        stage = next(x for x in plan['stages'] if x['id'] == stage_id)
        stage['default'] = member
        begin(s, c, link, plan, selected, path, '手动切换默认节点')
        c.execute('UPDATE links SET plan=? WHERE id=?', (dump(plan), ident))
    else:
        raise s.UserError('未知操作')
    c.execute('UPDATE links SET revision=revision+1 WHERE id=?', (ident,))
    return dict(id=ident)


def retire(s, c, lid, rules):
    for r in rules:
        s.reserve_removed(c, r['node_id'], r['port'])
        if s.get_node(c, r['node_id'])['first_seen']:
            c.execute('INSERT OR IGNORE INTO link_retired VALUES (?,?,?)', (lid, r['node_id'], r['port']))


def delete_link(s, c, ident):
    link = get(s, c, 'links', ident)
    if link['job']:
        raise s.UserError('链路正在切换，请先等待确认', status=409)
    if not link['managed'] and link['chain_id']:
        # Adopt the existing rules before requesting their removal.
        plan = json.loads(link['plan'])
        begin(s, c, link, plan, json.loads(link['selected']), [r[0] for r in c.execute('SELECT node_id FROM hops WHERE chain_id=? ORDER BY seq', (link['chain_id'],))], '删除准备')
    retire(s, c, ident, rows(c, ident))
    c.execute('DELETE FROM link_rules WHERE link_id=?', (ident,))
    c.execute("UPDATE links SET state='deleting',job=NULL,error='',revision=revision+1 WHERE id=?", (ident,))
    if link['chain_id']:
        c.execute('DELETE FROM hops WHERE chain_id=?', (link['chain_id'],))
        c.execute('UPDATE chains SET deleting=1 WHERE id=?', (link['chain_id'],))
    return dict(deleted=False)


def confirmed(s, c, nodes):
    for nid in nodes:
        n = s.get_node(c, nid)
        if (n['manual_required'] or n['state'] != 'ok' or n['applied_generation'] != n['desired_generation']
                or n['reported_v'] != s.rules_version(s.node_rules(c, n))):
            return False
    return True


def progress(s, c, link, now):
    job = json.loads(link['job'])
    if not confirmed(s, c, job['wait']):
        failures = [nid for nid in job['wait'] if c.execute("SELECT 1 FROM sync_ops o JOIN nodes n ON n.id=o.node_id WHERE n.id=? AND o.generation=n.desired_generation AND o.result='failed'", (nid,)).fetchone()]
        if failures and job['phase'] == 'prepare':
            staged = [dict(r) for r in c.execute("SELECT * FROM link_rules WHERE link_id=? AND phase='stage'", (link['id'],))]
            retire(s, c, link['id'], staged)
            c.execute("DELETE FROM link_rules WHERE link_id=? AND phase='stage'", (link['id'],))
            failed = list(set(json.loads(link['failures'])) | set(failures))
            c.execute("UPDATE links SET job=NULL,state='error',error=?,failures=? WHERE id=?", ('新路径准备失败，旧入口未切换；请查看节点错误', dump(failed), link['id']))
            event(c, link['id'], '新路径准备失败，已取消本次切换')
        elif failures or now - job['at'] >= WAIT_SECONDS:
            text = '同步失败，等待人工处理' if failures else '同步超时，等待手动同步或重试；结果尚未确认'
            c.execute("UPDATE links SET state='waiting',error=? WHERE id=?", (text, link['id']))
        return
    if job['phase'] == 'prepare':
        old_entry = next((r for r in job['old'] if c.execute('SELECT 1 FROM hops WHERE id=? AND seq=0', (r['id'],)).fetchone()), None)
        # An entry address change is explicit; remove its old rule only after the new entry is confirmed.
        if job['new']:
            entry = job['new'][0]
            if old_entry and (old_entry['node_id'], old_entry['port']) == (entry['node_id'], entry['port']):
                c.execute('DELETE FROM link_rules WHERE id=?', (old_entry['id'],))
            c.execute("INSERT INTO link_rules VALUES (?,?, 'stage',?,?,?,?)", (entry['id'], link['id'], entry['node_id'], entry['port'], entry['host'], entry['tport']))
            job['wait'] = [entry['node_id']]
        else:
            job['wait'] = []
        job.update(phase='commit', at=now)
        c.execute("UPDATE links SET job=?,state='switching',error='' WHERE id=?", (dump(job), link['id']))
        event(c, link['id'], '下游已确认，切换入口')
        return
    new_ids = {r['id'] for r in job['new']}
    old = [r for r in job['old'] if r['id'] not in new_ids]
    # The stable entry port is still owned by the new rule and must not be retired.
    live_ports = {(r['node_id'], r['port']) for r in job['new']}
    retire(s, c, link['id'], [r for r in old if (r['node_id'], r['port']) not in live_ports])
    c.execute("DELETE FROM link_rules WHERE link_id=? AND phase='live'", (link['id'],))
    c.execute("UPDATE link_rules SET phase='live' WHERE link_id=?", (link['id'],))
    c.execute('DELETE FROM hops WHERE chain_id=?', (link['chain_id'],))
    for index, r in enumerate(job['new']):
        c.execute('INSERT INTO hops VALUES (?,?,?,?,?)', (r['id'], link['chain_id'], index, r['node_id'], r['port']))
    c.execute('UPDATE chains SET target_id=?,remark=? WHERE id=?', (job['target'], link['name'], link['chain_id']))
    plan = json.loads(link['plan'])
    if job['new']: plan['port'] = job['new'][0]['port']
    c.execute("UPDATE links SET selected=?,plan=?,job=NULL,state='active',error='',revision=revision+1 WHERE id=?", (dump(job['selected']), dump(plan), link['id']))
    event(c, link['id'], '新路径已确认生效，旧规则开始清理')


def tick(s, c, now=None, allow_failover=True):
    now = int(time.time()) if now is None else now
    for r in c.execute('SELECT * FROM link_retired').fetchall():
        if confirmed(s, c, [r['node_id']]) and not c.execute('SELECT 1 FROM retired_ports WHERE node_id=? AND port=?', (r['node_id'], r['port'])).fetchone():
            c.execute('DELETE FROM link_retired WHERE link_id=? AND node_id=? AND port=?', (r['link_id'], r['node_id'], r['port']))
    for row in c.execute('SELECT * FROM links').fetchall():
        link = dict(row)
        if link['state'] == 'deleting':
            if not link['managed'] and link['chain_id'] and c.execute('SELECT 1 FROM chain_pending WHERE chain_id=?', (link['chain_id'],)).fetchone():
                continue
            if not c.execute('SELECT 1 FROM link_retired WHERE link_id=?', (link['id'],)).fetchone():
                c.execute('DELETE FROM links WHERE id=?', (link['id'],))
                if link['chain_id'] and c.execute('SELECT 1 FROM chains WHERE id=?', (link['chain_id'],)).fetchone():
                    s.delete_chain(c, link['chain_id'])
            continue
        if link['job']:
            progress(s, c, link, now)
            continue
        if link['state'] == 'active' and not c.execute('SELECT 1 FROM link_retired WHERE link_id=?', (link['id'],)).fetchone():
            c.execute("UPDATE links SET retained='[]' WHERE id=?", (link['id'],))
        if link['chain_id'] is None or not allow_failover:
            continue
        plan, selected = json.loads(link['plan']), json.loads(link['selected'])
        lost = []
        for stage in plan['stages']:
            nid = selected.get(stage['id']) if stage['kind'] == 'pool' else None
            if not nid: continue
            n = s.get_node(c, nid)
            if n['last_seen'] is not None and now - n['last_seen'] >= OFFLINE_AFTER:
                lost.append(nid)
        if lost:
            try:
                chosen, path = choose(s, c, link, plan, prefer_current=True, now=now)
                if chosen != selected:
                    begin(s, c, link, plan, chosen, path, '节点持续失联，自动灾备')
            except s.UserError as exc:
                error = str(exc)
                if exc.field and exc.field.startswith('capacity:'):
                    nid = int(exc.field.split(':')[1])
                    if any(st['kind'] == 'pool' and nid in st['order'] for st in plan['stages']):
                        failures = list(set(json.loads(link['failures'])) | {nid})
                        c.execute('UPDATE links SET failures=? WHERE id=?', (dump(failures), link['id']))
                if link['error'] != error: event(c, link['id'], '灾备等待：' + error)
                c.execute("UPDATE links SET state='error',error=? WHERE id=?", (error, link['id']))
        elif link['state'] == 'error' and not json.loads(link['failures']):
            c.execute("UPDATE links SET state='active',error='' WHERE id=?", (link['id'],))


def rule_rows(c, nid):
    return [dict(r) for r in c.execute('''SELECT r.id,r.port,r.host,r.tport,l.chain_id,c.remark AS chain_remark
        FROM link_rules r JOIN links l ON l.id=r.link_id JOIN chains c ON c.id=l.chain_id WHERE r.node_id=?''', (nid,))]


def display_state(s, c, link):
    state, error = link['state'], link['error']
    if state == 'active' and link['chain_id']:
        for h in c.execute('SELECT node_id FROM hops WHERE chain_id=? ORDER BY seq', (link['chain_id'],)):
            n = s.get_node(c, h['node_id'])
            status = s.node_status(c, n, int(time.time()))
            if status in ('error', 'offline', 'skew', 'deleting'):
                return 'error', f"节点 {n['name']}：" + (n['err'] or ('失联；池成员持续失联 60 秒后尝试灾备' if status == 'offline' else '状态异常'))
            if status != 'synced':
                state, error = 'pending', f"等待节点 {n['name']} 确认配置"
    return state, error


def sync_status(s, c, link):
    """UI-only receipt summary; scheduling and the stored link state stay unchanged."""
    state, _ = display_state(s, c, link)
    job = json.loads(link['job']) if link['job'] else None
    ids = job['wait'] if job else [r[0] for r in c.execute(
        'SELECT node_id FROM hops WHERE chain_id=? ORDER BY seq', (link['chain_id'],))]
    if not job and link['state'] == 'error':
        ids = list(dict.fromkeys(ids + json.loads(link['failures'])))
    pending = []
    for nid in ids:
        node = c.execute('SELECT * FROM nodes WHERE id=?', (nid,)).fetchone()
        if node is None:
            continue
        status = s.node_status(c, node, int(time.time()))
        if confirmed(s, c, [nid]) and status == 'synced':
            continue
        wake = s.wake.summary(c, node)
        current_wake = wake if wake and wake['generation'] == node['desired_generation'] else None
        if node['state'] == 'error' or (current_wake and current_wake['state'] == 'failed'):
            reason = 'failed'
        elif node['manual_required']:
            reason = 'manual'
        elif current_wake and current_wake['state'] == 'timeout':
            reason = 'timeout'
        elif node['desired_mode'] == 'passive' and not s.wake.eligible(node):
            reason = 'manual'
        elif status == 'offline':
            reason = 'offline'
        elif state == 'waiting':
            reason = 'timeout'
        else:
            reason = 'pending'
        pending.append(dict(id=nid, name=node['name'], mode=node['desired_mode'], reason=reason,
                            error=node['err'] or (current_wake['error'] if current_wake else '')))
    reasons = {n['reason'] for n in pending}
    if state not in ('draft', 'deleting'):
        state = next((x for x in ('failed', 'offline', 'timeout', 'manual') if x in reasons), state)
    return dict(state=state, nodes=pending, retry=state in ('failed', 'offline', 'timeout', 'error', 'waiting'))


def detail(s, c, link):
    result = dict(link)
    result['state'], result['error'] = display_state(s, c, link)
    result['sync'] = sync_status(s, c, link)
    for key in ('plan', 'selected', 'failures'):
        result[key] = json.loads(result[key])
    for stage in result['plan']['stages']:
        if stage['kind'] == 'pool':
            stage['default'] = default_member(stage)
    result.pop('retained', None)
    result['job'] = json.loads(result['job']) if result['job'] else None
    result['cleanup'] = [dict(r) for r in c.execute('SELECT node_id,port FROM link_retired WHERE link_id=?', (link['id'],))]
    result['events'] = [dict(r) for r in c.execute('SELECT ts,message FROM link_events WHERE link_id=? ORDER BY id DESC LIMIT 30', (link['id'],))]
    result['hops'] = [dict(r) for r in c.execute('SELECT h.*,n.name,n.addr FROM hops h JOIN nodes n ON n.id=h.node_id WHERE chain_id=? ORDER BY seq', (link['chain_id'],))]
    result['connection'] = None
    result['applied_target_id'] = None
    if link['chain_id'] and (result['hops'] or result['plan']['entry'] is None):
        chain = s.get_chain(c, link['chain_id']); target = s.get_target(c, chain['target_id'])
        result['applied_target_id'] = target['id']
        first = result['hops'][0] if result['hops'] else dict(addr=target['host'], port=target['port'])
        result['connection'] = s.target_config.connection(target, first['addr'], first['port'], link['name'])
    return result


def listing(s, c):
    return dict(links=[detail(s, c, r) for r in c.execute('SELECT * FROM links ORDER BY id')],
                pools=[dict(id=p['id'], name=p['name'], members=json.loads(p['members']),
                            references=list(dict.fromkeys(l['name'] for l, _ in references(c, p['id'])))) for p in c.execute('SELECT * FROM pools ORDER BY id')],
                nodes=[dict(id=n['id'], name=n['name'], addr=n['addr'], mode=n['desired_mode'],
                            eligible=n['installed_agent'] == 'active' and n['desired_mode'] == 'active' and not n['manual_required'] and not n['deleting'],
                            status=s.node_status(c, n, int(time.time()))) for n in c.execute('SELECT * FROM nodes')],
                targets=[dict(id=t['id'], name=t['name']) for t in c.execute('SELECT * FROM targets')])


def quality_paths(c, only=None):
    paths = {}
    edges = set()
    for link in c.execute("SELECT * FROM links WHERE state NOT IN ('draft','deleting')"):
        if only is not None and link['id'] != only:
            continue
        plan = json.loads(link['plan'])
        stages = [[plan['entry']]] if plan['entry'] else []
        for x in plan['stages']:
            stages.append([x['node_id']] if x['kind'] == 'node' else [n for n in x['order'] if n not in x['disabled']])
        previous = []
        all_nodes = []
        for options in stages:
            real = [n for n in options if n]
            edges.update((a, b) for a in previous for b in real if a != b)
            previous = list(dict.fromkeys(previous + real)) if 0 in options else real
            all_nodes.extend(real)
        paths[('links', link['id'])] = list(dict.fromkeys(all_nodes))
    return paths, edges


class Monitor:
    def __init__(self, hub):
        self.hub = hub
        self.task = None
        self.started = time.time()

    async def run(self):
        while True:
            try:
                def advance(c):
                    before = self.hub.s.mutation_snapshot(c)
                    tick(self.hub.s, c, allow_failover=time.time() - self.started >= OFFLINE_AFTER)
                    return self.hub.s.finish_mutation(c, before)
                if await self.hub.database(advance):
                    self.hub.notify()
            except asyncio.CancelledError:
                raise
            except Exception:
                logging.exception('Pool scheduler failed')
            await asyncio.sleep(1)
