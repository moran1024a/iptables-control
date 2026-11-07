"""Explicit node addresses. Saved addresses never reconfigure host interfaces."""
import ipaddress
import json
import re
import sqlite3
import time

import database
import errors
import settings
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


ROUTING_FIELDS = ('id', 'host', 'local', 'device', 'gateway')
DEVICE = re.compile(r'[A-Za-z0-9_.:-]{1,15}')


def routing(address):
    return {k: address.get(k, '') for k in ROUTING_FIELDS}


def write(c, nid, values):
    c.execute('INSERT INTO node_addresses VALUES (?,?) ON CONFLICT(node_id) DO UPDATE SET addresses=excluded.addresses',
              (nid, json.dumps(values, ensure_ascii=False)))


def primary_values(values, host):
    """Keep IDs and routing bindings intact when adopting the primary-address model."""
    values = [dict(a) for a in values]
    primary = next((a for a in values if a.get('host') == host), None)
    if primary is None:
        ident = 'main'
        while any(a.get('id') == ident for a in values):
            ident += '_'
        primary = dict(id=ident, name='', host=host, local='', device='', gateway='')
        try: primary['local'] = ipv4(host)
        except errors.UserError: pass  # Preserve legacy DNS contact addresses.
        values.append(primary)
    for a in values:
        a['primary'] = a is primary
    return values


def migrate_primary(c):
    if c.execute('PRAGMA user_version').fetchone()[0] >= 504:
        return
    database.backup_database(c, settings.DB_PATH + '.pre-node-addresses.bak')
    with c:
        for node in c.execute('SELECT * FROM nodes').fetchall():
            write(c, node['id'], primary_values(listing(c, node), node['addr']))
        c.execute('PRAGMA user_version=504')


def network(c, nid):
    row = c.execute('SELECT inventory,inventory_at FROM node_snapshots WHERE node_id=?', (nid,)).fetchone()
    inv = json.loads(row['inventory']) if row else {}
    candidates = []
    interfaces = inv.get('interfaces', [])
    complete = 'interfaces' in inv and isinstance(interfaces, list) and inv.get('network_complete', True) is True
    if not isinstance(interfaces, list):
        interfaces = []
    for interface in interfaces:
        if not isinstance(interface, dict) or not DEVICE.fullmatch(str(interface.get('name', ''))):
            continue
        addresses = interface.get('addresses', [])
        if not isinstance(addresses, list):
            continue
        for value in addresses:
            try:
                ip = ipv4(str(ipaddress.IPv4Interface(value).ip))
            except (ValueError, TypeError, errors.UserError):
                continue
            candidate = dict(local=ip, device=interface['name'])
            if candidate not in candidates:
                candidates.append(candidate)
    return dict(addresses=candidates, sampled_at=row['inventory_at'] if row and 'interfaces' in inv else None,
                complete=complete)


def resolve(address, candidates):
    """Only fill missing information; a later inventory must not rewire a live path."""
    if address['device'] or not address['local']:
        return
    devices = {a['device'] for a in candidates if a['local'] == address['local']}
    if len(devices) == 1:
        address['device'] = next(iter(devices))


def refresh_pending(c, nid):
    node = database.get_node(c, nid)
    values = listing(c, node)
    before = json.dumps(values)
    snapshot = network(c, nid)
    candidates = snapshot['addresses'] if snapshot['complete'] else []
    for a in values:
        resolve(a, candidates)
    if json.dumps(values) != before:
        write(c, nid, values)


def receive_network(c, nid, fields):
    """Small, bounded IPv4 inventory carried by the existing Bash sync envelope."""
    raw = fields.get('network_addresses')
    if raw is None:
        return
    if len(raw) > 4096 or len(raw.split(';')) > 96:
        raise errors.UserError('网络信息过大')
    interfaces = {}
    for item in raw.split(';'):
        if not item:
            continue
        device, sep, address = item.partition(',')
        if not sep or not DEVICE.fullmatch(device):
            raise errors.UserError('网络信息格式无效')
        try:
            value = ipaddress.IPv4Interface(address)
        except (ValueError, TypeError):
            raise errors.UserError('网络地址无效')
        interfaces.setdefault(device, []).append(str(value))
    row = c.execute('SELECT inventory FROM node_snapshots WHERE node_id=?', (nid,)).fetchone()
    inv = json.loads(row[0]) if row else {}
    inv.update(interfaces=[dict(name=k, addresses=v) for k, v in interfaces.items()],
               sampled_at=int(time.time() * 1000), network_complete=fields.get('network_complete') == '1')
    c.execute('INSERT OR IGNORE INTO node_snapshots(node_id) VALUES (?)', (nid,))
    c.execute('UPDATE node_snapshots SET inventory=?,inventory_at=? WHERE node_id=?',
              (json.dumps(inv), int(time.time()), nid))
    refresh_pending(c, nid)


def ready(address):
    return bool(address.get('local') and address.get('device'))


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


def save(c, node, values, *, addr=None):
    if not isinstance(values, list) or len(values) > 33:
        raise errors.UserError('每个节点最多配置 32 个副 IP', 'addresses')
    values = [dict(v) if isinstance(v, dict) else v for v in values]
    if not all(isinstance(v, dict) for v in values):
        raise errors.UserError('地址格式无效', 'addresses')
    explicit = any('primary' in a for a in values)
    if explicit:
        if any(type(a.get('primary', False)) is not bool for a in values) or sum(a.get('primary', False) for a in values) != 1:
            raise errors.UserError('请选择一个主 IP', 'addresses')
        primary = next(a for a in values if a.get('primary'))
        if addr is not None and addr != validation.check_host(primary.get('host'), 'addresses'):
            raise errors.UserError('主 IP 与节点地址不一致', 'addresses')
    else:
        values = primary_values(values, addr or node['addr'])
    if len(values) > 33:
        raise errors.UserError('每个节点最多配置 32 个副 IP', 'addresses')
    snapshot = network(c, node['id'])
    candidates = snapshot['addresses'] if snapshot['complete'] else []
    old = {a['id']: a for a in listing(c, node)}
    result, seen = [], set()
    for value in values:
        if not isinstance(value, dict):
            raise errors.UserError('地址格式无效', 'addresses')
        ident = value.get('id', '')
        if not isinstance(ident, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,48}', ident) or ident in seen:
            raise errors.UserError('地址标识无效或重复', 'addresses')
        seen.add(ident)
        device = value.get('device', '')
        if not isinstance(device, str) or device and not DEVICE.fullmatch(device):
            raise errors.UserError('网卡名称无效', 'addresses')
        primary = bool(value.get('primary'))
        existing_host = old.get(ident, {}).get('host')
        host = validation.check_host(value.get('host'), 'addresses') if primary or value.get('host') == existing_host else ipv4(value.get('host'))
        if primary:
            try: ipaddress.IPv4Address(host)
            except ValueError: pass
            else: host = ipv4(host)
        local = value.get('local')
        if not local:
            try: local = ipv4(host)
            except errors.UserError: local = ''
        address = dict(id=ident, primary=primary,
                       name=validation.check_remark(value['name'], 'addresses', '地址名称') if value.get('name') else '',
                       host=host, local=ipv4(local, optional=True), device=device,
                       gateway=ipv4(value.get('gateway'), optional=True))
        resolve(address, candidates)
        result.append(address)
    # Labels are presentation only; changing them must not interrupt forwarding.
    new = {a['id']: a for a in result}
    # An unresolved address may occur in a draft, but cannot have been published.
    # Let the user finish its local binding without first removing the draft edge.
    changed = {ident for ident, a in old.items() if ident not in new or ready(a) and routing(a) != routing(new[ident])}
    if changed:
        import topology
        for name, doc in topology.documents(c):
            graph = doc.get('graph', {})
            used = references(graph)
            if any(used.get(v['id'], set()) & changed for v in graph.get('vertices', []) if v.get('node_id') == node['id']):
                raise errors.UserError(f"地址正被链路「{name}」引用，请先修改链路", 'addresses')
    write(c, node['id'], result)
    host = next(a['host'] for a in result if a['primary'])
    c.execute('UPDATE nodes SET addr=? WHERE id=?', (host, node['id']))
    return host


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
