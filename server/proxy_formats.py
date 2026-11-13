"""Single-node share formats. Pure parsing/serialization; never fetch a URL."""

import base64
import ipaddress
import json
import re
import uuid
from urllib.parse import parse_qsl, quote, unquote, urlencode, urlsplit

PROTOCOLS = {"ss": "shadowsocks", "vmess": "vmess", "vless": "vless",
             "trojan": "trojan", "hy2": "hysteria2", "hysteria2": "hysteria2",
             "tuic": "tuic", "anytls": "anytls", "socks": "socks5", "socks5": "socks5",
             "http": "http", "https": "http"}
SS_METHODS = {"aes-128-gcm", "aes-192-gcm", "aes-256-gcm", "chacha20-ietf-poly1305",
              "xchacha20-ietf-poly1305", "2022-blake3-aes-128-gcm",
              "2022-blake3-aes-256-gcm", "2022-blake3-chacha20-poly1305"}


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def decode64(value):
    try:
        return base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)
    except ValueError:
        # binascii's English message is not something to show in the panel.
        raise ValueError("分享链接中的 Base64 内容无效") from None


def clean(value):
    if not isinstance(value, str) or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise ValueError("链接含有无效字符")
    return value


def host_value(value):
    clean(value)
    try:
        return str(ipaddress.ip_address(value))
    except ValueError:
        host = value.rstrip(".").encode("idna").decode("ascii")
        if len(host) > 253 or not host or any(not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", s) for s in host.split(".")):
            raise ValueError("无效的服务器地址")
        return host.lower()


def number(value, lo=1, hi=65535):
    if isinstance(value, bool) or not re.fullmatch(r"[0-9]{1,10}", str(value)) or not lo <= int(value) <= hi:
        raise ValueError("无效的数值或端口")
    return int(value)


def flag(value):
    value = str(value).lower()
    if value not in ("true", "false", "1", "0"):
        raise ValueError("无效的开关参数")
    return value in ("true", "1")


def encryption(value):
    if value == "none":
        return value
    parts = value.split(".")
    if len(parts) < 4 or parts[0] != "mlkem768x25519plus" or parts[1] not in ("native", "xorpub", "random") or parts[2] not in ("0rtt", "1rtt"):
        raise ValueError("无效的 VLESS ENC encryption 参数")
    keys = False
    for p in parts[3:]:
        if not keys and re.fullmatch(r"[0-9]+-[0-9]+-[0-9]+", p):
            chance, low, high = map(int, p.split("-"))
            if not 0 <= chance <= 100 or not 0 <= low <= high <= 65535:
                raise ValueError("无效的 ENC padding")
        else:
            if not re.fullmatch(r"[A-Za-z0-9_-]+", p) or len(decode64(p)) not in (32, 1184):
                raise ValueError("无效的 ENC 公钥")
            keys = True
    if not keys:
        raise ValueError("ENC 缺少公钥")
    return value


def unique_pairs(pairs):
    out = {}
    for k, v in pairs:
        if k in out:
            raise ValueError("链接含有重复字段")
        out[k] = v
    return out


def parse(uri):
    if not isinstance(uri, str) or not 1 <= len(uri.strip()) <= 8192:
        raise ValueError("请粘贴一条节点分享链接（最多 8192 字符）")
    uri = uri.strip()
    clean(uri)
    if re.search(r"%(?![0-9a-fA-F]{2})", uri):
        raise ValueError("无效的百分号编码")
    # SIP002 userinfo may be standard Base64; a "/" there would end the authority.
    m = re.match(r"(?i)(ss://)([A-Za-z0-9+/_=-]*/[A-Za-z0-9+/_=-]*)(@.*)", uri, re.S)
    if m:
        uri = m[1] + m[2].replace("/", "_").replace("+", "-") + m[3]
    try:
        u = urlsplit(uri)
        uri = u.scheme + uri[uri.index(":"):]
        protocol = PROTOCOLS.get(u.scheme)
        if not protocol:
            raise ValueError("不支持此分享协议")
        if u.scheme == "vmess" and "@" not in u.netloc:
            raw = json.loads(decode64(uri.split("://", 1)[1].split("#", 1)[0]).decode(), object_pairs_hook=unique_pairs)
            if not isinstance(raw, dict) or any(v is not None and not isinstance(v, (str, int)) for v in raw.values()):
                raise ValueError("无效的 VMess JSON")
            for k, v in raw.items():
                clean(k); clean(str(v))
            ident = str(uuid.UUID(clean(raw["id"])))
            number(raw.get("aid", 0), 0)
            return dict(name=clean(unquote(u.fragment, errors="strict")) or str(raw.get("ps") or raw["add"]),
                        host=host_value(raw["add"]), port=number(raw["port"]), protocol=protocol,
                        params=dict(uuid=ident, vmess=raw), uri=uri)
        # Legacy whole-authority Base64 links, including passwords containing @.
        if u.scheme in ("ss", "socks", "socks5") and "@" not in u.netloc:
            payload = uri.split("://", 1)[1].split("#", 1)[0].split("?", 1)[0]
            if ":" not in u.netloc and not u.netloc.startswith("["):
                decoded = clean(decode64(payload).decode())
                auth, endpoint = decoded.rsplit("@", 1)
                user, password = auth.split(":", 1)
                u = urlsplit(f"{u.scheme}://{quote(user, safe='')}:{quote(password, safe='')}@{endpoint}"
                             + ("?" + u.query if u.query else "") + ("#" + u.fragment if u.fragment else ""))
        if u.path not in ("", "/") or not u.hostname or any(c.isspace() for c in u.netloc):
            raise ValueError("请填写单个代理地址，不支持订阅 URL")
        host = host_value(u.hostname)
        port = number(u.port if u.port is not None else {"hy2":443, "hysteria2":443, "http":80, "https":443}.get(u.scheme))
        pairs = parse_qsl(u.query, keep_blank_values=True, max_num_fields=64, errors="strict")
        q = unique_pairs(pairs)
        for k, v in pairs:
            clean(k); clean(v)
        raw_auth, sep, _ = u.netloc.rpartition("@")
        auth = clean(unquote(raw_auth, errors="strict")) if sep else ""
        p = dict(share_query=pairs)
        if protocol in ("shadowsocks", "socks5", "http"):
            if auth and ":" not in auth and protocol != "http":
                auth = clean(decode64(auth).decode())
            user, separator, password = auth.partition(":")
            if ":" in raw_auth:
                raw_user, _, raw_password = raw_auth.partition(":")
                user, password = unquote(raw_user, errors="strict"), unquote(raw_password, errors="strict")
            if protocol == "shadowsocks":
                if user not in SS_METHODS or not separator or not password:
                    raise ValueError("无效的 SS 加密方式或密码")
                if user.startswith("2022-"):
                    size = 16 if user == "2022-blake3-aes-128-gcm" else 32
                    if any(len(decode64(k)) != size for k in password.split(":")):
                        raise ValueError("无效的 SS2022 密钥")
                p.update(method=user, password=password)
            else:
                if auth and not separator:
                    raise ValueError("认证信息应为 用户名:密码")
                p.update(username=user, password=password, tls=u.scheme == "https")
        elif protocol in ("vless", "vmess"):
            p["uuid"] = str(uuid.UUID(auth))
            if protocol == "vless":
                encryption(q.get("encryption") or "none")
                if q.get("security", "none") not in ("", "none", "tls", "reality"):
                    raise ValueError("无效的 VLESS security 参数")
                if q.get("security") == "reality":
                    if (not q.get("sni") or not re.fullmatch(r"[A-Za-z0-9_-]{43}=?", q.get("pbk", ""))
                            or len(decode64(q["pbk"])) != 32
                            or not re.fullmatch(r"(?:[0-9a-fA-F]{2}){0,8}", q.get("sid", ""))):
                        raise ValueError("Reality 需要 sni、有效 pbk 和 sid")
        elif protocol == "tuic":
            ident, separator, password = auth.partition(":")
            if not separator or not password:
                raise ValueError("TUIC v5 需要 UUID 和密码")
            p.update(uuid=str(uuid.UUID(ident)), password=password)
        else:
            if not auth and protocol != "hysteria2":
                raise ValueError("缺少代理密码")
            p["password"] = auth
        for key in ("insecure", "allowInsecure", "allow_insecure", "skip-cert-verify", "disable_sni", "reduce_rtt"):
            if key in q:
                flag(q[key])
        return dict(name=clean(unquote(u.fragment, errors="strict")) or host, host=host, port=port,
                    protocol=protocol, params=p, uri=uri)
    except (KeyError, IndexError, TypeError, UnicodeError, OverflowError, RecursionError) as exc:
        raise ValueError("分享链接无效：请检查地址、端口和认证信息") from None


def endpoint(host, port):
    return f"[{host}]:{port}" if ":" in host else f"{host}:{port}"


def share(node, name, host=None, port=None):
    """Preserve original fields. Only VMess JSON requires decoding to rename."""
    p = node["params"]
    host, port = host or node["host"], port or node["port"]
    if "vmess" in p:
        raw = dict(p["vmess"], ps=name, add=host, port=port)
        return "vmess://" + base64.b64encode(canonical(raw).encode()).decode()
    if node.get("uri") and (host, port) == (node["host"], node["port"]):
        return node["uri"].split("#", 1)[0] + "#" + quote(name, safe="")
    q = dict(p.get("share_query", []))
    if node["protocol"] == "shadowsocks":
        auth = (quote(p["method"], safe="") + ":" + quote(p["password"], safe="")
                if p["method"].startswith("2022-") else
                base64.urlsafe_b64encode((p["method"] + ":" + p["password"]).encode()).decode().rstrip("="))
        scheme = "ss"
    elif node["protocol"] == "vless":
        auth, scheme = p["uuid"], "vless"
        if q.get("security") in ("tls", "reality") and not q.get("sni"):
            q["sni"] = node["host"]
    else:
        raise ValueError("此协议不支持替换转发入口")
    return f"{scheme}://{auth}@{endpoint(host, port)}" + ("?" + urlencode(q) if q else "") + "#" + quote(name, safe="")


def link_error(node):
    if node["protocol"] == "http":
        return "v2rayN 不支持 HTTP/HTTPS 分享链接；可用 Mihomo"
    return ""


def allowed(q, keys):
    unknown = set(q) - set(keys)
    if unknown:
        raise ValueError("Mihomo 暂不支持转换参数：" + ", ".join(sorted(unknown)))


TLS_KEYS = {"sni", "peer", "alpn", "fp", "insecure", "allowInsecure", "allow_insecure", "skip-cert-verify"}
STREAM_KEYS = {"type", "host", "path", "serviceName", "headerType"}


def tls_options(out, q, node, enabled, fingerprint=True):
    if not enabled:
        if any(q.get(k) for k in TLS_KEYS):
            raise ValueError("TLS 参数需要启用 TLS")
        return
    key = "servername" if out["type"] in ("vless", "vmess") else "sni"
    out[key] = q.get("sni") or q.get("peer") or node["host"]
    values = [flag(q[k]) for k in ("insecure", "allowInsecure", "allow_insecure", "skip-cert-verify") if k in q]
    if values:
        if len(set(values)) != 1:
            raise ValueError("证书校验参数冲突")
        out["skip-cert-verify"] = values[0]
    if q.get("alpn"):
        out["alpn"] = q["alpn"].split(",")
    if q.get("fp"):
        if not fingerprint:
            raise ValueError("此协议不支持转换 TLS 指纹参数")
        out["client-fingerprint"] = q["fp"]


def stream_options(out, q):
    network = q.get("type") or "tcp"
    if network == "raw":
        network = "tcp"
    if network not in ("tcp", "ws", "grpc", "http", "h2", "httpupgrade") or q.get("headerType", "none") not in ("", "none"):
        raise ValueError("Mihomo 暂不支持转换此传输方式")
    if network == "tcp" and any(q.get(k) for k in ("host", "path", "serviceName")):
        raise ValueError("TCP 传输含有无法转换的伪装参数")
    if network in ("ws", "httpupgrade"):
        out["network"] = "ws"
        opts = {"path": q.get("path") or "/"}
        if q.get("host"):
            opts["headers"] = {"Host": q["host"]}
        if network == "httpupgrade":
            opts["v2ray-http-upgrade"] = True
        out["ws-opts"] = opts
    elif network == "grpc":
        if q.get("host") or q.get("path"):
            raise ValueError("暂不支持转换 gRPC authority/path，请使用 serviceName")
        out.update(network="grpc", **{"grpc-opts": {"grpc-service-name": q.get("serviceName", "")}})
    elif network in ("http", "h2"):
        out["network"] = network
        out[network + "-opts"] = ({"path": [q.get("path") or "/"], "headers": {"Host": q.get("host", "").split(",")}}
                                  if network == "http" else {"path": q.get("path") or "/", "host": q.get("host", "").split(",")})


def mihomo(node, name):
    p, protocol = node["params"], node["protocol"]
    q = dict(p.get("share_query", []))
    out = dict(name=name, type="ss" if protocol == "shadowsocks" else protocol,
               server=node["host"], port=node["port"], udp=protocol != "http")
    if protocol == "shadowsocks":
        allowed(q, {"plugin"})
        out.update(cipher=p["method"], password=p["password"])
        if q.get("plugin"):
            plugin, *parts = q["plugin"].split(";")
            opts = unique_pairs([s.split("=", 1) if "=" in s else (s, True) for s in parts if s])
            if plugin in ("obfs-local", "simple-obfs") and set(opts) <= {"obfs", "obfs-host"}:
                if opts.get("obfs") not in ("http", "tls"):
                    raise ValueError("无效的 obfs 模式")
                out.update(plugin="obfs", **{"plugin-opts": {"mode":opts["obfs"], "host":opts.get("obfs-host", "")}})
            elif plugin == "v2ray-plugin" and set(opts) <= {"mode", "tls", "host", "path", "mux"}:
                if opts.get("mode", "websocket") != "websocket":
                    raise ValueError("Mihomo 仅支持 v2ray-plugin websocket 模式")
                out.update(plugin=plugin, **{"plugin-opts": dict(mode="websocket", tls=flag(opts.get("tls", False)),
                           host=opts.get("host", ""), path=opts.get("path", "/"), mux=flag(opts.get("mux", False)))})
            else:
                raise ValueError("Mihomo 暂不支持转换此 SS 插件")
        return out
    if protocol == "vmess" and "vmess" in p:
        v = p["vmess"]
        allowed(v, {"v", "ps", "add", "port", "id", "aid", "scy", "net", "type", "host", "path", "tls", "sni", "alpn", "fp", "insecure"})
        q = {"type":v.get("net") or "tcp", "security":v.get("tls") or "none"}
        q.update({k:str(v[k]) for k in ("sni", "alpn", "fp", "insecure", "host", "path") if v.get(k) not in ("", None) and k in v})
        if q["type"] == "grpc":
            q["serviceName"] = q.pop("path", "")
        if v.get("type") not in (None, "", "none"):
            raise ValueError("暂不支持转换此 VMess 传输模式")
        out.update(alterId=number(v.get("aid", 0), 0), cipher=v.get("scy") or "auto")
        if out['cipher'] not in ('auto','aes-128-gcm','chacha20-poly1305','none','zero'):
            raise ValueError('Mihomo 暂不支持此 VMess 加密方式')
    if protocol in ("vless", "vmess", "trojan"):
        allowed(q, TLS_KEYS | STREAM_KEYS | {"security"} | ({"encryption", "flow", "pbk", "sid", "spx"} if protocol == "vless" else set()))
        security = q.get("security") or ("tls" if protocol == "trojan" else "none")
        if security not in ("none", "tls", "reality") or security == "reality" and protocol != "vless":
            raise ValueError("不支持此安全模式")
        if protocol == "trojan":
            if security != "tls":
                raise ValueError("Trojan 导出需要 TLS")
            out["password"] = p["password"]
        else:
            out.update(uuid=p["uuid"], tls=security != "none")
            if protocol == "vmess":
                out.setdefault("alterId", 0); out.setdefault("cipher", "auto")
            else:
                out["encryption"] = encryption(q.get("encryption") or "none")
                if q.get("flow"):
                    if q["flow"] != "xtls-rprx-vision":
                        raise ValueError("不支持此 VLESS flow")
                    out["flow"] = q["flow"]
        tls_options(out, q, node, security != "none")
        if security == "reality":
            out["reality-opts"] = {"public-key":q["pbk"], "short-id":q.get("sid", "")}
            out.setdefault("client-fingerprint", "chrome")
            if q.get("spx"):
                raise ValueError("Mihomo 暂不支持转换 spiderX")
        elif any(q.get(k) for k in ("pbk", "sid", "spx")):
            raise ValueError("Reality 参数需要启用 Reality")
        stream_options(out, q)
        return out
    if protocol in ("socks5", "http"):
        allowed(q, {"sni", "insecure", "skip-cert-verify"})
        out.update(username=p["username"], password=p["password"], tls=p["tls"])
        tls_options(out, q, node, p["tls"])
        return out
    extra = ({"obfs", "obfs-password", "pinSHA256", "mport", "up", "down"} if protocol == "hysteria2" else
             {"congestion_control", "udp_relay_mode", "disable_sni", "reduce_rtt"} if protocol == "tuic" else set())
    allowed(q, (TLS_KEYS - {"fp"}) | extra)
    out["password"] = p["password"]
    tls_options(out, q, node, True, fingerprint=False)
    if protocol == "hysteria2":
        for direction in ('up','down'):
            if q.get(direction):
                if not re.fullmatch(r'[0-9]+(?:\.[0-9]+)?(?:\s*[KMGT]?bps)?', q[direction], re.I):
                    raise ValueError('无效的 Hysteria2 带宽参数')
                out[direction] = q[direction]
        if q.get("obfs"):
            if q["obfs"] != "salamander" or not q.get("obfs-password"):
                raise ValueError("Hysteria2 仅支持带密码的 salamander 混淆")
            out.update(obfs=q["obfs"], **{"obfs-password":q["obfs-password"]})
        elif q.get("obfs-password"):
            raise ValueError("缺少 Hysteria2 混淆方式")
        if q.get("pinSHA256"):
            pin = q["pinSHA256"].replace(":", "")
            if not re.fullmatch(r"[0-9a-fA-F]{64}", pin):
                raise ValueError("无效的证书 SHA256")
            out["fingerprint"] = pin
        if q.get("mport"):
            for part in q["mport"].split(","):
                nums = part.split("-")
                if len(nums) > 2 or number(nums[0]) > number(nums[-1]):
                    raise ValueError("无效的跳跃端口")
            out["ports"] = q["mport"]
    elif protocol == "tuic":
        out["uuid"] = p["uuid"]
        for key, dest, choices in (("congestion_control", "congestion-controller", ("cubic", "new_reno", "bbr")),
                                    ("udp_relay_mode", "udp-relay-mode", ("native", "quic"))):
            if key in q:
                if q[key] not in choices:
                    raise ValueError("无效的 TUIC 参数")
                out[dest] = q[key]
        for key in ("disable_sni", "reduce_rtt"):
            if key in q:
                out[key.replace("_", "-")] = flag(q[key])
    return out
