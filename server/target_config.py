"""Managed targets use the same formats as external imports, with a narrower policy."""
import json
import proxy_formats

canonical = proxy_formats.canonical
decode64 = proxy_formats.decode64


def import_uri(uri):
    node = proxy_formats.parse(uri)
    if node['protocol'] == 'vless':
        q = dict(node['params']['share_query'])
        if q.get('security') == 'reality':
            raise ValueError('目标不再支持 VLESS Reality，请导入 VLESS ENC')
        if q.get('encryption', 'none') in ('', 'none'):
            raise ValueError('VLESS 目标需要有效的 ENC encryption 参数')
        # Endpoint aliases and forwarded ingress must not change the remote service identity.
        if q.get('security') == 'tls' and not q.get('sni'):
            q['sni'] = node['host']
        if (q.get('type') in ('ws', 'httpupgrade', 'http', 'h2', 'xhttp')
                or q.get('headerType') == 'http') and not q.get('host'):
            q['host'] = node['host']
        node['params']['share_query'] = list(q.items())
        node['protocol'] = 'vless-enc'
    elif node['protocol'] != 'shadowsocks':
        raise ValueError('目标仅支持 SS、SS2022 和 VLESS ENC 分享链接')
    return {k: node[k] for k in ('name', 'host', 'port', 'protocol', 'params')}


def connection(target, host, port, name):
    protocol = target['type']
    out = dict(protocol=protocol, host=host, port=port)
    if protocol == 'endpoint':
        out['uri'] = proxy_formats.endpoint(host, port)
        return out
    if protocol not in ('shadowsocks', 'vless-enc'):
        raise ValueError('不支持的目标协议')
    params = json.loads(target['params'])
    node = dict(protocol='vless' if protocol == 'vless-enc' else protocol,
                host=target.get('host', host) if isinstance(target, dict) else target['host'],
                port=target.get('port', port) if isinstance(target, dict) else target['port'], params=params)
    out['uri'] = proxy_formats.share(node, name, host, port)
    out.update({k: params[k] for k in ('method', 'password', 'uuid') if k in params})
    return out
