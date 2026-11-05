"""Selected link configurations, exported without a panel session."""
import base64
import json
import secrets

import pools
import target_config

SCHEMA = """
CREATE TABLE IF NOT EXISTS subscriptions (
 id INTEGER PRIMARY KEY, name TEXT NOT NULL, token TEXT NOT NULL UNIQUE,
 enabled INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS subscription_links (
 subscription_id INTEGER NOT NULL REFERENCES subscriptions(id) ON DELETE CASCADE,
 link_id INTEGER NOT NULL REFERENCES links(id) ON DELETE CASCADE,
 PRIMARY KEY(subscription_id,link_id)
);
"""


def clash_proxy(target, connection, name):
    p = json.loads(target['params'])
    q = dict(p.get('share_query', []))
    out = dict(name=name, server=connection['host'], port=connection['port'], udp=True)
    if target['type'] == 'shadowsocks':
        unknown = set(q) - {'plugin'}
        if unknown:
            raise ValueError('Mihomo 暂不支持转换参数：' + ', '.join(sorted(unknown)))
        out.update(type='ss', cipher=p['method'], password=p['password'])
        if q.get('plugin'):
            plugin, *options = q['plugin'].split(';')
            opts = dict(part.split('=', 1) if '=' in part else (part, True) for part in options if part)
            if plugin in ('obfs-local', 'simple-obfs') and set(opts) <= {'obfs', 'obfs-host'}:
                if opts.get('obfs') not in ('http', 'tls'):
                    raise ValueError('无效的 obfs 模式')
                out.update(plugin='obfs', **{'plugin-opts': {'mode': opts['obfs'], 'host': opts.get('obfs-host', '')}})
            elif plugin == 'v2ray-plugin' and set(opts) <= {'mode', 'tls', 'host', 'path', 'mux'}:
                if opts.get('mode', 'websocket') != 'websocket':
                    raise ValueError('Mihomo 仅支持 v2ray-plugin websocket 模式')
                out.update(plugin=plugin, **{'plugin-opts': dict(mode='websocket', tls='tls' in opts,
                           host=opts.get('host', ''), path=opts.get('path', '/'), mux='mux' in opts)})
            else:
                raise ValueError('Mihomo 暂不支持转换此 SS 插件')
        return out
    if target['type'] != 'vless-reality':
        raise ValueError('仅有地址和端口，缺少代理协议')
    unknown = set(q) - {'encryption', 'security', 'sni', 'fp', 'pbk', 'sid', 'type', 'flow',
                        'alpn', 'host', 'path', 'serviceName', 'headerType', 'spx'}
    if unknown or q.get('encryption', 'none') not in ('none', ''):
        raise ValueError('Mihomo 暂不支持转换此 VLESS 参数')
    network = q.get('type', 'tcp')
    if network == 'raw':
        network = 'tcp'
    if network not in ('tcp', 'ws', 'grpc', 'http', 'h2', 'httpupgrade') or q.get('headerType', 'none') != 'none':
        raise ValueError('Mihomo 暂不支持转换此传输方式')
    out.update(type='vless', uuid=p['uuid'], tls=True, servername=p['server_name'],
               **{'client-fingerprint': q.get('fp') or 'chrome',
                  'reality-opts': {'public-key': p['public_key'], 'short-id': p['short_id']}})
    flow = q.get('flow', '' if 'share_query' in p else 'xtls-rprx-vision')
    if flow:
        out['flow'] = flow
    if q.get('alpn'):
        out['alpn'] = q['alpn'].split(',')
    if network in ('ws', 'httpupgrade'):
        out['network'] = 'ws'
        opts = {'path': q.get('path', '/')}
        if q.get('host'):
            opts['headers'] = {'Host': q['host']}
        if network == 'httpupgrade':
            opts['v2ray-http-upgrade'] = True
        out['ws-opts'] = opts
    elif network == 'grpc':
        out.update(network='grpc', **{'grpc-opts': {'grpc-service-name': q.get('serviceName', '')}})
    elif network in ('http', 'h2'):
        out['network'] = network
        out[network + '-opts'] = ({'path': [q.get('path', '/')], 'headers': {'Host': q.get('host', '').split(',')}}
                                  if network == 'http' else {'path': q.get('path', '/'), 'host': q.get('host', '').split(',')})
    return out


def configurations(s, c):
    result = []
    for link in c.execute(s.ordered('links')):
        if not link['chain_id']:
            continue
        chain = s.get_chain(c, link['chain_id'])
        target = s.get_target(c, chain['target_id'])
        info = pools.detail(s, c, link)
        connection = info['connection'] if link['state'] != 'deleting' else None
        error = ('配置正在删除' if link['state'] == 'deleting' else '配置尚未生效') if not connection else ''
        if target['type'] == 'endpoint':
            error = '仅有地址和端口，缺少代理协议'
        item = dict(id=link['id'], name=link['name'], group_id=link['group_id'],
                    connection=connection, target=dict(target), error=error, clash_error=error)
        if not error:
            try:
                clash_proxy(target, connection, link['name'])
            except ValueError as exc:
                item['clash_error'] = str(exc)
        result.append(item)
    return result


def listing(s, c):
    configs = configurations(s, c)
    rows = []
    for row in c.execute('SELECT * FROM subscriptions ORDER BY id'):
        ids = [r[0] for r in c.execute('SELECT link_id FROM subscription_links WHERE subscription_id=?', (row['id'],))]
        selected = [x for x in configs if x['id'] in ids]
        rows.append(dict(row, enabled=bool(row['enabled']), link_ids=ids,
                         path='/sub/' + row['token'],
                         counts={fmt: sum(not x[field] for x in selected)
                                 for fmt, field in (('v2ray', 'error'), ('clash', 'clash_error'))}))
    return dict(subscriptions=rows, configs=[{k: x[k] for k in ('id', 'name', 'group_id', 'error', 'clash_error')} for x in configs],
                groups=s.get_groups(c, 'links'), public_url=s.setting(c, 'public_url', ''))


def save(s, c, body, ident=None):
    old = pools.get(s, c, 'subscriptions', ident) if ident else None
    name = s.check_remark(body.get('name', old['name'] if old else ''), 'name', '名称')
    enabled = body.get('enabled', bool(old['enabled']) if old else True)
    if type(enabled) is not bool:
        raise s.UserError('启用状态无效', 'enabled')
    if body.get('reset_token', False) not in (True, False):
        raise s.UserError('重置参数无效')
    ids = body.get('link_ids')
    if ids is not None:
        if not isinstance(ids, list) or len(ids) > 512 or any(type(n) is not int for n in ids) or len(set(ids)) != len(ids):
            raise s.UserError('请选择不重复的配置，最多 512 项', 'link_ids')
        available = {x['id']: x for x in configurations(s, c)}
        for nid in ids:
            if nid not in available:
                raise s.UserError('配置已删除，请刷新后重试', 'link_ids')
    if old:
        token = secrets.token_hex(32) if body.get('reset_token') else old['token']
        c.execute('UPDATE subscriptions SET name=?,enabled=?,token=? WHERE id=?', (name, int(enabled), token, ident))
    else:
        ident = c.execute('INSERT INTO subscriptions(name,token,enabled) VALUES (?,?,?)',
                          (name, secrets.token_hex(32), int(enabled))).lastrowid
    if ids is not None:
        c.execute('DELETE FROM subscription_links WHERE subscription_id=?', (ident,))
        c.executemany('INSERT INTO subscription_links VALUES (?,?)', [(ident, n) for n in ids])
    return dict(id=ident)


def export(s, c, token, fmt):
    row = c.execute('SELECT * FROM subscriptions WHERE token=? AND enabled=1', (token,)).fetchone()
    if row is None:
        return None
    ids = {r[0] for r in c.execute('SELECT link_id FROM subscription_links WHERE subscription_id=?', (row['id'],))}
    items = [x for x in configurations(s, c) if x['id'] in ids and not x['error']]
    names = {'DIRECT', 'REJECT', 'GLOBAL', '订阅节点'}
    links, proxies = [], []
    for item in items:
        name = item['name']
        while name in names:
            name += ' [' + str(item['id']) + ']'
        names.add(name)
        if fmt == 'clash':
            if not item['clash_error']:
                proxies.append(clash_proxy(item['target'], item['connection'], name))
        else:
            links.append(target_config.connection(item['target'], item['connection']['host'], item['connection']['port'], name)['uri'])
    if fmt == 'v2ray':
        return base64.b64encode('\n'.join(links).encode()).decode() + '\n', 'text/plain; charset=utf-8'
    # JSON flow values are a YAML subset; quote every user-controlled string.
    group = dict(name='订阅节点', type='select', proxies=[p['name'] for p in proxies] or ['REJECT'])
    encode = lambda obj: json.dumps(obj, ensure_ascii=True, separators=(',', ':'))
    body = 'mixed-port: 7890\nallow-lan: false\nmode: rule\nproxies:'
    body += ('\n' + ''.join('  - ' + encode(p) + '\n' for p in proxies)) if proxies else ' []\n'
    body += 'proxy-groups:\n  - ' + encode(group) + '\nrules:\n  - ' + encode('MATCH,订阅节点') + '\n'
    return body, 'application/yaml; charset=utf-8'
