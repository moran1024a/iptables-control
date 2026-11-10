"""Mixed topology/external subscriptions; format conversion lives in proxy_formats."""
import base64
import json
import secrets
import database
import errors
import configurations as configs
import proxy_formats
import validation


def get(c, ident):
    row = c.execute("SELECT * FROM subscriptions WHERE id=?", (ident,)).fetchone()
    if row is None:
        raise errors.UserError("订阅不存在", status=404)
    return row


def catalog(c):
    listing = configs.configs(c)
    items = []
    for item in listing['configs']:
        connection = item['connection']
        row = {k:item[k] for k in ('id','source','name','code','group_id')}
        row.update(error='', clash_error='', node=None)
        error = '配置尚未生效' if not connection else '仅有地址和端口，缺少代理协议' if connection['protocol'] == 'endpoint' else ''
        if not error:
            try:
                row['node'] = proxy_formats.parse(connection['uri'])
            except ValueError as exc:
                error = str(exc)
        if error:
            row.update(error=error, clash_error=error)
        else:
            row['error'] = proxy_formats.link_error(row['node'])
            try:
                proxy_formats.mihomo(row['node'], row['name'])
            except ValueError as exc:
                row['clash_error'] = str(exc)
        items.append(row)
    return items, listing['groups']


MEMBERS = (('topology', 'configuration_ids', 'subscription_configurations', 'configuration_id'),
           ('external', 'external_configuration_ids', 'subscription_external_configurations', 'external_configuration_id'))


def selections(c, ident):
    return {field:[r[0] for r in c.execute(f'SELECT {column} FROM {table} WHERE subscription_id=?', (ident,))]
            for source,field,table,column in MEMBERS}


def selected(items, ids):
    wanted = {source:set(ids[field]) for source,field,_,_ in MEMBERS}
    return [x for x in items if x['id'] in wanted[x['source']]]


def listing(c):
    items, groups = catalog(c)
    rows = []
    for row in c.execute('SELECT * FROM subscriptions ORDER BY id'):
        ids = selections(c, row['id'])
        members = selected(items, ids)
        rows.append(dict(row, **ids, enabled=bool(row['enabled']), path='/sub/'+row['token'],
                         counts={fmt:sum(not x[field] for x in members)
                                 for fmt,field in (('v2ray','error'),('clash','clash_error'))}))
    return dict(subscriptions=rows, configs=[{k:v for k,v in x.items() if k != 'node'} for x in items],
                groups=groups, public_url=database.setting(c,'public_url',''))


def save(c, body, ident=None):
    old = get(c, ident) if ident else None
    name = validation.check_remark(body.get('name', old['name'] if old else ''), 'name', '名称')
    enabled = body.get('enabled', bool(old['enabled']) if old else True)
    if type(enabled) is not bool:
        raise errors.UserError('启用状态无效','enabled')
    if type(body.get('reset_token', False)) is not bool:
        raise errors.UserError('重置参数无效')
    ids = selections(c, ident) if old else {field:[] for _,field,_,_ in MEMBERS}
    for source,field,table,column in MEMBERS:
        if field not in body:
            continue
        values = body[field]
        if not isinstance(values,list) or len(values)>512 or any(type(v) is not int for v in values) or len(set(values))!=len(values):
            raise errors.UserError('请选择不重复的配置，最多 512 项',field)
        target = 'configurations' if source == 'topology' else 'external_configurations'
        available = {r[0] for r in c.execute(f'SELECT id FROM {target}' + (' WHERE deleted=0' if source=='topology' else ''))}
        if not set(values) <= available:
            raise errors.UserError('配置已删除，请刷新后重试',field)
        ids[field] = values
    if sum(map(len,ids.values())) > 512:
        raise errors.UserError('最多绑定 512 项配置')
    if old:
        token = secrets.token_hex(32) if body.get('reset_token') else old['token']
        c.execute('UPDATE subscriptions SET name=?,enabled=?,token=? WHERE id=?',(name,int(enabled),token,ident))
    else:
        ident = c.execute('INSERT INTO subscriptions(name,token,enabled) VALUES (?,?,?)',(name,secrets.token_hex(32),int(enabled))).lastrowid
    for source,field,table,column in MEMBERS:
        if field in body:
            c.execute(f'DELETE FROM {table} WHERE subscription_id=?',(ident,))
            c.executemany(f'INSERT INTO {table} VALUES (?,?)',[(ident,n) for n in ids[field]])
    return dict(id=ident)


def export(c, token, fmt):
    row = c.execute('SELECT * FROM subscriptions WHERE token=? AND enabled=1',(token,)).fetchone()
    if row is None:
        return None
    items, _ = catalog(c)
    names = {'DIRECT','REJECT','GLOBAL','订阅节点'}
    links, proxies = [], []
    for item in selected(items,selections(c,row['id'])):
        if item['clash_error' if fmt=='clash' else 'error']:
            continue
        name = item['name']
        while fmt=='clash' and name in names:
            name += ' ['+item['code']+']'
        names.add(name)
        if fmt=='clash':
            proxies.append(proxy_formats.mihomo(item['node'],name))
        else:
            links.append(proxy_formats.share(item['node'],name))
    if fmt=='v2ray':
        return base64.b64encode('\n'.join(links).encode()).decode()+'\n','text/plain; charset=utf-8'
    group = dict(name='订阅节点',type='select',proxies=[p['name'] for p in proxies] or ['REJECT'])
    encode = lambda obj: json.dumps(obj,ensure_ascii=True,separators=(',',':'))
    body = 'mixed-port: 7890\nallow-lan: false\nmode: rule\nproxies:'
    body += '\n'+''.join('  - '+encode(p)+'\n' for p in proxies) if proxies else ' []\n'
    body += 'proxy-groups:\n  - '+encode(group)+'\nrules:\n  - '+encode('MATCH,订阅节点')+'\n'
    return body,'application/yaml; charset=utf-8'
