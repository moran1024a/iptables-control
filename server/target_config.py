"""外部目标分享链接解析与生成，不管理代理核心。"""
import base64
import json
import re
import uuid
from urllib.parse import parse_qsl, quote, unquote, urlencode, urlsplit


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def decode64(value):
    return base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)


def import_uri(uri):
    """只解析分享链接，不连接远端；保留客户端所需的传输、插件等查询参数。"""
    if not isinstance(uri, str) or not 1 <= len(uri.strip()) <= 8192:
        raise ValueError("请粘贴一条 ss:// 或 vless:// Reality 链接（最多 8192 字符）")
    uri = uri.strip()
    if any(ord(c) < 32 or ord(c) == 127 for c in uri) or re.search(r'%(?![0-9a-fA-F]{2})', uri):
        raise ValueError("链接含有无效字符或百分号编码")
    try:
        u = urlsplit(uri)
        if u.scheme not in ("ss", "vless"):
            raise ValueError
        # 兼容旧式 ss://base64(method:password@host:port)#name。
        if u.scheme == "ss" and "@" not in u.netloc:
            payload = uri[5:].split("#", 1)[0].split("?", 1)[0]
            decoded = decode64(payload).decode("utf-8")
            if any(c in decoded for c in "?#\r\n"):
                raise ValueError
            auth, endpoint = decoded.rsplit("@", 1)
            method, password = auth.split(":", 1)
            u = urlsplit(f"ss://{quote(method, safe='')}:{quote(password, safe='')}@{endpoint}"
                         + ("?" + u.query if u.query else "") + ("#" + u.fragment if u.fragment else ""))
        if u.path not in ("", "/") or not u.hostname or u.port is None or not 1 <= u.port <= 65535:
            raise ValueError
        auth, sep, _ = u.netloc.rpartition("@")
        if not sep or not auth or any(c.isspace() for c in u.netloc):
            raise ValueError
        query = parse_qsl(u.query, keep_blank_values=True, max_num_fields=64, errors="strict")
        if len({k for k, _ in query}) != len(query):
            raise ValueError
        if any(ord(c) < 32 or ord(c) == 127 for pair in query for value in pair for c in value):
            raise ValueError
        q = dict(query)
        if u.scheme == "ss":
            decoded = unquote(auth, errors="strict")
            if ":" not in decoded:
                decoded = decode64(decoded).decode("utf-8")
            method, password = decoded.split(":", 1)
            methods = {"aes-128-gcm", "aes-192-gcm", "aes-256-gcm", "chacha20-ietf-poly1305",
                       "xchacha20-ietf-poly1305", "2022-blake3-aes-128-gcm",
                       "2022-blake3-aes-256-gcm", "2022-blake3-chacha20-poly1305"}
            if method not in methods or not password or any(ord(c) < 32 or ord(c) == 127 for c in password):
                raise ValueError
            if method.startswith("2022-"):
                size = 16 if method == "2022-blake3-aes-128-gcm" else 32
                if any(len(decode64(key)) != size for key in password.split(":")):
                    raise ValueError
            protocol = "shadowsocks"
            params = {"method": method, "password": password, "share_query": query}
        else:
            if q.get("security") != "reality" or not q.get("sni") or not q.get("pbk"):
                raise ValueError
            public = q["pbk"]
            sid = q.get("sid", "")
            if (not re.fullmatch(r'[A-Za-z0-9_-]{43}=?', public) or len(decode64(public)) != 32
                    or not re.fullmatch(r'(?:[0-9a-fA-F]{2}){0,8}', sid)):
                raise ValueError
            protocol = "vless-reality"
            params = {"uuid": str(uuid.UUID(unquote(auth))), "server_name": q["sni"],
                      "public_key": public, "short_id": sid, "share_query": query}
        return {"name": unquote(u.fragment, errors="strict") or u.hostname, "host": u.hostname,
                "port": u.port, "protocol": protocol, "params": params}
    except (ValueError, UnicodeError):
        raise ValueError("分享链接无效：请检查协议、地址、端口及凭据；SS2022 需有效密钥，Reality 需 UUID、sni、pbk 和有效 sid") from None


def connection(target, host, port, name):
    p = json.loads(target["params"])
    protocol = target["type"]
    out = {"protocol": protocol, "host": host, "port": port}
    if protocol == "shadowsocks":
        # SIP002：AEAD-2022 必须使用百分号编码的明文 userinfo，不能再套 Base64URL。
        auth = (f"{quote(p['method'], safe='')}:{quote(p['password'], safe='')}" if p['method'].startswith("2022-")
                else base64.urlsafe_b64encode(f"{p['method']}:{p['password']}".encode()).decode().rstrip("="))
        query = "/?" + urlencode(dict(p["share_query"])) if p.get("share_query") else ""
        out.update(method=p["method"], password=p["password"],
                   uri=f"ss://{auth}@{host}:{port}{query}#{quote(name, safe='')}")
    elif protocol == "endpoint":
        out["uri"] = f"{host}:{port}"
    else:
        params = dict(p["share_query"]) if "share_query" in p else {"encryption": "none", "security": "reality", "sni": p["server_name"],
                  "fp": "chrome", "pbk": p["public_key"], "sid": p["short_id"],
                  "type": "tcp", "flow": "xtls-rprx-vision"}
        out.update(uuid=p["uuid"], server_name=p["server_name"], public_key=p["public_key"], short_id=p["short_id"],
                   uri=f"vless://{p['uuid']}@{host}:{port}?{urlencode(params)}#{quote(name, safe='')}")
    return out
