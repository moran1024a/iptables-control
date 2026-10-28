"""固定的 sing-box 模板；不接受任意核心配置或执行命令。"""

import base64
import hashlib
import json
import secrets
import subprocess
import uuid
from urllib.parse import quote, urlencode


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def revision(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def reality_keys():
    # X25519 DER 的末尾 32 字节为原始密钥，兼容 sing-box 的 URL-safe base64 格式。
    private = subprocess.run(["openssl", "genpkey", "-algorithm", "X25519", "-outform", "DER"],
                             capture_output=True, check=True).stdout
    public = subprocess.run(["openssl", "pkey", "-inform", "DER", "-pubout", "-outform", "DER"],
                            input=private, capture_output=True, check=True).stdout
    encode = lambda data: base64.urlsafe_b64encode(data[-32:]).decode().rstrip("=")
    return encode(private), encode(public)


def new_credentials(protocol):
    if protocol == "shadowsocks":
        return {"method": "2022-blake3-aes-128-gcm", "password": base64.b64encode(secrets.token_bytes(16)).decode()}
    if protocol == "socks5":
        return {"username": "ictl", "password": secrets.token_urlsafe(24)}
    private, public = reality_keys()
    return {"uuid": str(uuid.uuid4()), "private_key": private, "public_key": public,
            "short_id": secrets.token_hex(8)}


def inbound(tunnel):
    p = json.loads(tunnel["params"])
    protocol = tunnel["protocol"]
    result = {"type": "socks" if protocol == "socks5" else "vless" if protocol == "vless-reality" else protocol,
              "tag": f"ictl-{tunnel['id']}", "listen": "0.0.0.0", "listen_port": tunnel["port"]}
    if protocol == "shadowsocks":
        result.update(method=p["method"], password=p["password"])
    elif protocol == "socks5":
        result["users"] = [{"username": p["username"], "password": p["password"]}]
    else:
        result["users"] = [{"uuid": p["uuid"], "flow": "xtls-rprx-vision"}]
        result["tls"] = {"enabled": True, "server_name": p["server_name"], "reality": {
            "enabled": True, "handshake": {"server": p["handshake_host"], "server_port": p["handshake_port"]},
            "private_key": p["private_key"], "short_id": [p["short_id"]]}}
    return result


def core_config(tunnels):
    return {"log": {"level": "warn", "timestamp": True},
            "inbounds": [inbound(t) for t in tunnels],
            "outbounds": [{"type": "direct", "tag": "direct"}]}


def connection(tunnel, host, port, name):
    p = json.loads(tunnel["params"])
    protocol = tunnel["protocol"]
    out = {"protocol": protocol, "host": host, "port": port}
    if protocol == "shadowsocks":
        auth = base64.urlsafe_b64encode(f"{p['method']}:{p['password']}".encode()).decode().rstrip("=")
        out.update(method=p["method"], password=p["password"],
                   uri=f"ss://{auth}@{host}:{port}#{quote(name, safe='')}")
    elif protocol == "socks5":
        out.update(username=p["username"], password=p["password"], tcp_only=True,
                   uri=f"socks5://{quote(p['username'], safe='')}:{quote(p['password'], safe='')}@{host}:{port}")
    else:
        params = {"encryption": "none", "security": "reality", "sni": p["server_name"],
                  "fp": "chrome", "pbk": p["public_key"], "sid": p["short_id"],
                  "type": "tcp", "flow": "xtls-rprx-vision"}
        out.update(uuid=p["uuid"], server_name=p["server_name"], public_key=p["public_key"], short_id=p["short_id"],
                   uri=f"vless://{p['uuid']}@{host}:{port}?{urlencode(params)}#{quote(name, safe='')}")
    return out
