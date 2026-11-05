"""Active-agent protocol, migration receipts and durable traffic high-water marks."""
import base64
import hashlib
import json
from pathlib import Path
import re
import secrets
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS node_snapshots (
 node_id INTEGER PRIMARY KEY, inventory TEXT NOT NULL DEFAULT '{}', metrics TEXT NOT NULL DEFAULT '{}',
 ports TEXT NOT NULL DEFAULT '{}', inventory_at INTEGER, metrics_at INTEGER, ports_at INTEGER
);
CREATE TABLE IF NOT EXISTS traffic_epochs (
 node_id INTEGER NOT NULL, rule_id INTEGER NOT NULL, protocol TEXT NOT NULL, epoch TEXT NOT NULL,
 up TEXT NOT NULL, down TEXT NOT NULL, up_packets TEXT NOT NULL, down_packets TEXT NOT NULL,
 updated INTEGER NOT NULL, quality TEXT NOT NULL, PRIMARY KEY(node_id,rule_id,protocol,epoch)
);
CREATE TABLE IF NOT EXISTS metric_windows (
 node_id INTEGER NOT NULL, window INTEGER NOT NULL, data TEXT NOT NULL,
 PRIMARY KEY(node_id,window)
);
CREATE TABLE IF NOT EXISTS metric_hours (
 node_id INTEGER NOT NULL, window INTEGER NOT NULL, data TEXT NOT NULL,
 PRIMARY KEY(node_id,window)
);
CREATE TABLE IF NOT EXISTS archived_nodes (
 node_id INTEGER PRIMARY KEY, name TEXT NOT NULL, deleted_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS agent_switches (
 node_id INTEGER PRIMARY KEY, op TEXT NOT NULL, target TEXT NOT NULL, generation INTEGER NOT NULL,
 hash TEXT NOT NULL, config TEXT NOT NULL, result TEXT, updated INTEGER NOT NULL
);
"""


def reset_v03(s, conn):
    """Retain node/target resources, but start 0.4 with no legacy topology.

    Port tombstones are cleanup bookkeeping, not retained forwarding configs.
    They prevent reuse until the old Agent confirms its new empty snapshot.
    """
    conn.execute('''INSERT OR IGNORE INTO retired_ports(node_id,port)
        SELECT h.node_id,h.port FROM hops h JOIN nodes n ON n.id=h.node_id WHERE n.first_seen IS NOT NULL''')
    for table in ('hops', 'chains', 'routes', 'chain_pending', 'sync_ops', 'deleted_receipts',
                  'windows', 'active_windows', 'events'):
        conn.execute(f'DELETE FROM {table}')
    conn.execute("DELETE FROM item_groups WHERE kind NOT IN ('nodes','targets')")
    for kind in ('routes', 'chains'):
        conn.execute('INSERT INTO item_groups(kind,name,builtin) VALUES (?,?,1)', (kind, s.DEFAULT_GROUP))
    conn.execute('''UPDATE nodes SET desired_generation=desired_generation+1,applied_generation=0,deleting=0,
        reported_v=NULL,reported_mode=NULL,state='init',err='',health=NULL,skew=NULL,
        src_ip=NULL,last_seen=NULL,
        last_manual=NULL,last_success=NULL''')
    s.set_setting(conn, 'active_window_start', str(int(time.time())))


def migrate(s, conn, reset_legacy=False):
    if conn.execute('PRAGMA user_version').fetchone()[0] == 400:
        return
    s.backup_database(conn, s.DB_PATH + '.pre-0.4.0.bak')
    with conn:
        conn.execute('BEGIN IMMEDIATE')
        columns = {r[1] for r in conn.execute('PRAGMA table_info(nodes)')}
        for name, ddl in {
            'installed_agent': "TEXT NOT NULL DEFAULT 'legacy'",
            'backend': "TEXT NOT NULL DEFAULT 'iptables'",
            'installation_id': 'TEXT', 'boot_id': 'TEXT',
            'capabilities': "TEXT NOT NULL DEFAULT '[]'", 'connection_state': "TEXT NOT NULL DEFAULT 'unknown'",
        }.items():
            if name not in columns:
                conn.execute(f'ALTER TABLE nodes ADD COLUMN {name} {ddl}')
        for statement in SCHEMA.split(';'):
            if statement.strip():
                conn.execute(statement)
        if reset_legacy:
            reset_v03(s, conn)
        conn.execute('PRAGMA user_version=400')


def fields(text):
    return dict(line.split('=', 1) for line in text.splitlines() if '=' in line)


def newer(candidate, current):
    def parts(value):
        m = re.fullmatch(r'(\d+)\.(\d+)\.(\d+)(?:-rc\.(\d+))?', value or '')
        return (*map(int, m.groups()[:3]), int(m[4]) if m[4] else float('inf')) if m else None
    a, b = parts(candidate), parts(current)
    return a is not None and b is not None and a > b


def artifact(s, name):
    allowed = {'ictl-active-linux-amd64', 'ictl-active-linux-arm64', 'ictl-wake-linux-amd64',
               'ictl-wake-linux-arm64', 'passive.sh', 'manage.sh', 'control.sh', 'wake.sh'}
    if name not in allowed:
        raise s.UserError('无效组件名称')
    root = Path(s.BASE_DIR)
    candidates = [root / 'assets' / name, root.parent / 'dist' / name]
    if name in ('passive.sh', 'manage.sh', 'control.sh', 'wake.sh'):
        candidates += [root / 'agent' / name, root.parent / 'agent' / name]
    path = next((p for p in candidates if p.is_file()), None)
    if path is None:
        raise s.UserError('组件尚未构建：' + name, status=503)
    return path, hashlib.sha256(path.read_bytes()).hexdigest()


def packet(s, conn, node, source='auto', op=None, status=None):
    status = status or {}
    data = {'t': str(int(time.time())), 'proto': '2', 'source': source,
            'phase': 'begin' if source == 'manual' else 'poll', 'op': op or secrets.token_hex(16),
            'agent': status.get('version', s.COMPONENTS['active_agent']), 'au': '1' if status.get('auto_update') else '0',
            'mode': 'active', 'state': status.get('state', 'ok'), 'err': str(status.get('error', ''))[:500],
            'generation': str(status.get('generation', node['applied_generation'])),
            'v': status.get('hash', node['reported_v'] or '')}
    result = fields(s.sync_report(conn, node, data, status.get('source_ip', '')))
    if result.get('action') == 'apply':
        result['rules'] = s.node_rule_rows(conn, node)
        result['generation'] = int(result['generation'])
        result['hash'] = result.pop('v')
    return result


def acknowledge(s, conn, node, data, source='auto'):
    required = ('op', 'generation', 'hash')
    if not all(k in data for k in required):
        raise s.UserError('缺少配置确认字段')
    payload = {'t': str(int(time.time())), 'proto': '2', 'source': source, 'phase': 'ack',
               'op': str(data['op']), 'generation': str(data['generation']), 'v': str(data['hash']),
               'mode': data.get('mode', 'active'), 'state': 'ok' if data.get('ok') is True else 'error',
               'err': str(data.get('error', ''))[:500], 'au': '0',
               'agent': data.get('version', s.COMPONENTS['active_agent'])}
    # Old flows keep their reservation even after the new snapshot is acknowledged.
    held = data.get('held_ports', [])
    if not isinstance(held, list) or len(held) > 65535 or any(type(p) is not int or not 1 <= p <= 65535 for p in held):
        raise s.UserError('无效的保留端口')
    result = fields(s.sync_report(conn, node, payload, ''))
    if result.get('result') == 'confirmed':
        for port in held:
            conn.execute('INSERT OR IGNORE INTO retired_ports VALUES (?,?)', (node['id'], port))
    return result


def refresh_held_ports(s, conn, node, data):
    """A repeated complete snapshot repairs lost releases without clearing a newer wait."""
    held = data.get('held_ports')
    if not isinstance(held, list) or any(type(p) is not int or not 1 <= p <= 65535 for p in held):
        raise s.UserError('无效的保留端口')
    if (node['installed_agent'] != 'active' or node['manual_required'] or node['desired_mode'] != 'active'
            or node['applied_generation'] != node['desired_generation']
            or data.get('generation') != node['applied_generation']
            or data.get('hash') != node['reported_v']):
        return
    conn.execute('DELETE FROM retired_ports WHERE node_id=?', (node['id'],))
    conn.executemany('INSERT OR IGNORE INTO retired_ports VALUES (?,?)', ((node['id'], p) for p in held))


def save_snapshot(s, conn, nid, kind, data):
    if kind not in ('inventory', 'metrics', 'ports') or not isinstance(data, dict):
        raise s.UserError('无效的采集信息')
    encoded = json.dumps(data, ensure_ascii=False, separators=(',', ':'))
    if len(encoded.encode()) > 512 * 1024:
        raise s.UserError('采集信息过大')
    now = int(time.time())
    conn.execute('INSERT OR IGNORE INTO node_snapshots(node_id) VALUES (?)', (nid,))
    conn.execute(f'UPDATE node_snapshots SET {kind}=?,{kind}_at=? WHERE node_id=?', (encoded, now, nid))
    if kind == 'metrics':
        values = {k: data[k] for k in ('cpu_percent', 'agent_rss') if type(data.get(k)) in (int,float)}
        mem = data.get('memory', {})
        if mem.get('MemTotal') and type(mem.get('MemAvailable')) in (int,float):
            values['memory_used_percent'] = 100 * (1 - mem['MemAvailable'] / mem['MemTotal'])
        for table, size, keep in [('metric_windows',60,10080), ('metric_hours',3600,720)]:
            previous = conn.execute(f'SELECT data FROM {table} WHERE node_id=? AND window=?', (nid,now//size)).fetchone()
            aggregate = json.loads(previous[0]) if previous else {'samples':0,'first_at':now,'values':{}}
            aggregate['samples'] += 1
            aggregate['last_at'] = now
            for key, value in values.items():
                stat = aggregate['values'].setdefault(key, {'count':0,'sum':0,'min':value,'max':value})
                stat['count'] += 1
                stat['sum'] += value
                stat['min'], stat['max'] = min(stat['min'],value), max(stat['max'],value)
                stat['mean'] = stat['sum']/stat['count']
            conn.execute(f'INSERT OR REPLACE INTO {table} VALUES (?,?,?)', (nid,now//size,json.dumps(aggregate)))
            conn.execute(f'DELETE FROM {table} WHERE node_id=? AND window<?', (nid,now//size-keep))


def accept_traffic(s, conn, nid, rows):
    if not isinstance(rows, list) or len(rows) > 10000:
        raise s.UserError('无效统计批次')
    keys = ('up', 'down', 'up_packets', 'down_packets')
    acknowledgments = []
    for item in rows:
        if (not isinstance(item, dict) or type(item.get('rule_id')) is not int or item['rule_id'] < 1
                or item.get('protocol') not in ('tcp', 'udp')
                or not re.fullmatch(r'[a-f0-9]{32}', str(item.get('epoch', '')))):
            raise s.UserError('无效计数器身份')
        values = []
        for key in keys:
            value = item.get(key)
            if not isinstance(value, str) or not re.fullmatch(r'0|[1-9][0-9]{0,19}', value) or int(value) >= 2**64:
                raise s.UserError('无效累计值')
            values.append(int(value))
        ident = (nid, item['rule_id'], item['protocol'], item['epoch'])
        old = conn.execute('SELECT * FROM traffic_epochs WHERE node_id=? AND rule_id=? AND protocol=? AND epoch=?', ident).fetchone()
        if old:
            values = [max(value, int(old[key])) for key, value in zip(keys, values)]
        quality = str(item.get('quality', 'ok'))[:160]
        conn.execute('INSERT OR REPLACE INTO traffic_epochs VALUES (?,?,?,?,?,?,?,?,?,?)',
                     (*ident, *(str(v) for v in values), int(time.time()), quality))
        acknowledgments.append({'rule_id': item['rule_id'], 'protocol': item['protocol'], 'epoch': item['epoch'],
                                **dict(zip(keys,map(str,values)))})
    return acknowledgments


def telemetry(conn, nid):
    row = conn.execute('SELECT * FROM node_snapshots WHERE node_id=?', (nid,)).fetchone()
    out = dict(row) if row else {}
    for key in ('inventory', 'metrics', 'ports'):
        out[key] = json.loads(out.get(key) or '{}')
    totals = {}
    for row in conn.execute('SELECT * FROM traffic_epochs WHERE node_id=?', (nid,)):
        key = (row['rule_id'], row['protocol'])
        item = totals.setdefault(key, {'rule_id': key[0], 'protocol': key[1], 'up': 0, 'down': 0,
                                      'up_packets': 0, 'down_packets': 0, 'updated': 0, 'quality': 'ok'})
        for field in ('up', 'down', 'up_packets', 'down_packets'):
            item[field] += int(row[field])
        item['updated'] = max(item['updated'], row['updated'])
        if row['quality'] != 'ok':
            item['quality'] = row['quality']
    for item in totals.values():
        for field in ('up', 'down', 'up_packets', 'down_packets'):
            item[field] = str(item[field])
    out['traffic'] = list(totals.values())
    out['history'] = [{'time': r['window'] * 60, 'data': json.loads(r['data'])} for r in
                      conn.execute('SELECT * FROM metric_windows WHERE node_id=? ORDER BY window DESC LIMIT 120', (nid,))][::-1]
    out['hourly_history'] = [{'time':r['window']*3600,'data':json.loads(r['data'])} for r in
                            conn.execute('SELECT * FROM metric_hours WHERE node_id=? ORDER BY window DESC LIMIT 720',(nid,))][::-1]
    return out


def manage(s, conn, node, data):
    """Authenticated v2 envelope: available to the small Bash installation manager."""
    now = int(time.time())
    if not str(data.get('t', '')).isdigit() or abs(now - int(data['t'])) > s.MAX_SKEW:
        raise s.UserError('节点时间不正确')
    action = data.get('manage')
    if action == 'asset':
        path, _ = artifact(s, data.get('name'))
        return path.read_bytes()
    if action == 'info':
        return f"proto=2\nmode={node['desired_mode']}\ninstalled={node['installed_agent']}\n"
    if action == 'wake-assets':
        arch = data.get('arch')
        if arch not in ('amd64', 'arm64'):
            raise s.UserError('远程唤醒组件只支持 amd64 / arm64')
        name = f'ictl-wake-linux-{arch}'
        _, digest = artifact(s, name)
        _, script_digest = artifact(s, 'wake.sh')
        return f'proto=2\nwake_artifact={name}\nwake_sha256={digest}\nwake_script_sha256={script_digest}\n'
    if action == 'prepare':
        target = data.get('target')
        if target not in ('active', 'passive') or target != node['desired_mode']:
            raise s.UserError('目标类型与面板不一致')
        arch = data.get('arch')
        if target == 'active' and arch not in ('amd64', 'arm64'):
            raise s.UserError('不支持的架构')
        name = f'ictl-active-linux-{arch}' if target == 'active' else 'passive.sh'
        _, digest = artifact(s, name)
        _, manager_digest = artifact(s, 'manage.sh')
        _, passive_digest = artifact(s, 'passive.sh')
        if node['installed_agent'] != target:
            conn.execute('UPDATE nodes SET manual_required=1 WHERE id=?',(node['id'],))
        op = secrets.token_hex(16)
        result = packet(s, conn, node, 'manual', op)
        config = 'v=' + result['hash'] + '\n' + '\n'.join(s.node_rules(conn, node)) + '\n'
        conn.execute('INSERT OR REPLACE INTO agent_switches VALUES (?,?,?,?,?,?,NULL,?)',
                     (node['id'], op, target, result['generation'], result['hash'], config, now))
        version = s.COMPONENTS[target + '_agent']
        return (f"proto=2\nop={op}\nmode={target}\ngeneration={result['generation']}\nv={result['hash']}\n"
                f"artifact={name}\nsha256={digest}\nmanager_sha256={manager_digest}\npassive_sha256={passive_digest}\nversion={version}\n"
                f"config_b64={base64.b64encode(config.encode()).decode()}\n")
    record = conn.execute('SELECT * FROM agent_switches WHERE node_id=? AND op=?',
                          (node['id'], data.get('op', ''))).fetchone()
    if not record:
        raise s.UserError('找不到切换事务')
    if action == 'status':
        return f"proto=2\nresult={record['result'] or 'pending'}\nmode={record['target']}\n"
    if action not in ('commit', 'failed') or data.get('target') != record['target']:
        raise s.UserError('无效切换确认')
    if record['result'] == 'confirmed':
        current = record['target'] == node['desired_mode'] and record['generation'] == node['desired_generation']
        return 'proto=2\nresult=' + ('confirmed' if current else 'changed') + '\n'
    # A lost receipt can be retried after the normal ten-minute sync window.
    # Reconstitute only this persisted manual operation, never an arbitrary client snapshot.
    conn.execute('''INSERT OR REPLACE INTO sync_ops(node_id,op,source,generation,mode,hash,rules,expires)
        VALUES (?,?,'manual',?,?,?,?,?)''', (node['id'], record['op'], record['generation'], record['target'],
        record['hash'], record['config'].partition('\n')[2].rstrip('\n'), now + 600))
    result = acknowledge(s, conn, node, {'op': record['op'], 'generation': record['generation'],
                         'hash': record['hash'], 'mode': record['target'], 'ok': action == 'commit',
                         'error': data.get('error', '本机交接失败，已尝试恢复原配置') if action == 'failed' else '',
                         'held_ports': json.loads(data.get('held_ports', '[]')),
                         'version': data.get('agent', '')}, 'manual').get('result')
    if result == 'confirmed':
        conn.execute("UPDATE nodes SET installed_agent=?,backend=?,connection_state='disconnected' WHERE id=?",
                     (record['target'], 'nftables' if record['target'] == 'active' else 'iptables', node['id']))
        conn.execute('UPDATE agent_switches SET result=?,updated=? WHERE node_id=?', (result, now, node['id']))
    return f'proto=2\nresult={result}\n'
