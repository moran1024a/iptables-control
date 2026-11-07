"""Explicit node addresses. Saved addresses never reconfigure host interfaces."""
import ipaddress
import json
import re
import sqlite3

import database
import errors
import validation


def migrate(c):
    c.execute("CREATE TABLE IF NOT EXISTS node_addresses(node_id INTEGER PRIMARY KEY REFERENCES nodes(id) ON DELETE CASCADE, addresses TEXT NOT NULL)")
    c.execute("CREATE TABLE IF NOT EXISTS rule_routes(rule_id INTEGER PRIMARY KEY, route TEXT NOT NULL)")
    c.execute("CREATE TABLE IF NOT EXISTS link_members(link_id INTEGER PRIMARY KEY REFERENCES links(id) ON DELETE CASCADE, root_id INTEGER NOT NULL REFERENCES links(id), entry TEXT NOT NULL, target TEXT NOT NULL, position INTEGER NOT NULL, UNIQUE(root_id,entry,target))")
    c.execute("CREATE INDEX IF NOT EXISTS link_members_root ON link_members(root_id)")
    c.execute("CREATE TABLE IF NOT EXISTS link_batches(root_id INTEGER PRIMARY KEY REFERENCES links(id) ON DELETE CASCADE, members TEXT NOT NULL, graph TEXT NOT NULL, path TEXT NOT NULL, removed TEXT NOT NULL DEFAULT '[]', phase TEXT NOT NULL DEFAULT 'prepare')")
    if 'kind' not in {r['name'] for r in c.execute('PRAGMA table_info(link_batches)')}:
        c.execute("ALTER TABLE link_batches ADD COLUMN kind TEXT NOT NULL DEFAULT 'publish'")
    c.execute("CREATE TABLE IF NOT EXISTS topology_canvases(root_id INTEGER PRIMARY KEY REFERENCES links(id) ON DELETE CASCADE, graph TEXT NOT NULL, path TEXT NOT NULL, applied_graph TEXT, applied_path TEXT)")
    if not database.setting(c, 'independent_path_history', ''):
        # Old views shared one section for all entrances. Copy the bindings,
        # not samples: measurements remain shared by their exact task identity.
        for member in c.execute('SELECT * FROM link_members').fetchall():
            prefix = 'target:' + member['target'] + ':'
            for binding in c.execute("SELECT * FROM probe_bindings WHERE scope='links' AND owner_id=? AND section NOT LIKE 'config:%'", (member['root_id'],)).fetchall():
                section = binding['section']
                if section.startswith('target:'):
                    if not section.startswith(prefix): continue
                    section = section[len(prefix):]
                c.execute('''INSERT INTO probe_bindings(scope,owner_id,section,protocol,task_id,path_key,path_label,status,note,started,ended)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?)''', ('links',member['root_id'],f"config:{member['link_id']}:{section}",
                    *(binding[k] for k in ('protocol','task_id','path_key','path_label','status','note','started','ended'))))
        database.set_setting(c, 'independent_path_history', '1')
    cleanup_routes(c)
    if c.execute('PRAGMA user_version').fetchone()[0] < 501:
        c.execute('PRAGMA user_version=501')


def listing(c, node):
    row = c.execute("SELECT addresses FROM node_addresses WHERE node_id=?", (node['id'],)).fetchone()
    return json.loads(row[0]) if row else []


def ipv4(value, optional=False):
    if optional and not value:
        return ''
    try:
        ip = ipaddress.IPv4Address(value)
        if ip.is_unspecified or ip.is_multicast or ip.is_loopback:
            raise ValueError()
        return str(ip)
    except (ValueError, TypeError, ipaddress.AddressValueError):
        raise errors.UserError('请填写可用的 IPv4 地址', 'addresses')


def save(c, node, values):
    if not isinstance(values, list) or len(values) > 32:
        raise errors.UserError('每个节点最多配置 32 个地址', 'addresses')
    result, seen = [], set()
    for value in values:
        if not isinstance(value, dict):
            raise errors.UserError('地址格式无效', 'addresses')
        ident = value.get('id', '')
        if not isinstance(ident, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,48}', ident) or ident in seen:
            raise errors.UserError('地址标识无效或重复', 'addresses')
        seen.add(ident)
        device = value.get('device', '')
        if not isinstance(device, str) or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,15}', device):
            raise errors.UserError('请填写实际网卡名称', 'addresses')
        result.append(dict(id=ident, name=validation.check_remark(value.get('name'), 'addresses', '地址名称'),
                           host=ipv4(value.get('host')), local=ipv4(value.get('local') or value.get('host')),
                           device=device, gateway=ipv4(value.get('gateway'), optional=True)))
    old = {a['id']: a for a in listing(c, node)}
    # Labels are presentation only; changing them must not interrupt forwarding.
    routing = lambda a: {k: v for k, v in a.items() if k != 'name'}
    new = {a['id']: a for a in result}
    changed = {ident for ident, a in old.items() if ident not in new or routing(a) != routing(new[ident])}
    if changed:
        import topology
        for name, doc in topology.documents(c):
            graph = doc.get('graph', {})
            used = references(graph)
            if any(used.get(v['id'], set()) & changed for v in graph.get('vertices', []) if v.get('node_id') == node['id']):
                raise errors.UserError(f"地址正被链路「{name}」引用，请先修改链路", 'addresses')
    c.execute('INSERT INTO node_addresses VALUES (?,?) ON CONFLICT(node_id) DO UPDATE SET addresses=excluded.addresses',
              (node['id'], json.dumps(result, ensure_ascii=False)))


def get(c, node_id, ident):
    node = database.get_node(c, node_id)
    return next((a for a in listing(c, node) if a['id'] == ident), None)


def references(graph):
    """Addresses actually bound to vertices and edges, excluding the inventory."""
    used = {v['id']: {v['address']} if v.get('address') else set() for v in graph.get('vertices', [])}
    for mapping in graph.get('bindings', {}).values():
        for side in ('from', 'to'):
            for vertex, address in mapping.get(side, {}).items():
                if vertex in used:
                    used[vertex].add(address)
    return used


def cleanup_routes(c):
    # New ingress and rollback rules can exist only in a transaction journal.
    retained = {r['id'] for row in c.execute('SELECT job FROM links WHERE job IS NOT NULL')
                for key in ('old', 'new') for r in json.loads(row[0]).get(key, [])}
    orphaned = [r[0] for r in c.execute('''SELECT rule_id FROM rule_routes r
        WHERE NOT EXISTS (SELECT 1 FROM hops h WHERE h.id=r.rule_id)
          AND NOT EXISTS (SELECT 1 FROM link_rules l WHERE l.id=r.rule_id)''') if r[0] not in retained]
    c.executemany('DELETE FROM rule_routes WHERE rule_id=?', ((ident,) for ident in orphaned))


def edge_key(a, b):
    return a + '/' + b


def route_token(incoming, outgoing):
    return ','.join((incoming['local'], outgoing['local'], incoming['device'], outgoing['device'],
                     incoming['gateway'] or '-', outgoing['gateway'] or '-'))


def rule_route(c, rule_id):
    try:
        row = c.execute('SELECT route FROM rule_routes WHERE rule_id=?', (rule_id,)).fetchone()
    except sqlite3.OperationalError as exc:
        if 'no such table: rule_routes' in str(exc): return ''  # During an older schema migration.
        raise
    return row[0] if row else ''


def ingress_host(c, link, default):
    applied = json.loads(link['applied'])['plan'] if link['applied'] else {}
    return applied.get('ingress_host') or default
