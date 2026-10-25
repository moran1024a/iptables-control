#!/usr/bin/env python3
"""ictl 服务端原型：节点同步接口 + 命令行管理。

数据库路径默认 /var/lib/ictl-server/ictl.db，可用环境变量 ICTL_DB 覆盖。
"""

import argparse
import base64
import hashlib
import hmac
import http.server
import ipaddress
import os
import re
import secrets
import shutil
import sqlite3
import subprocess
import sys
import time
import unicodedata

VERSION = "0.1"
DB_PATH = os.environ.get("ICTL_DB", "/var/lib/ictl-server/ictl.db")
OFFLINE_AFTER = 30
MAX_SKEW = 60
MAX_BODY = 8192
NOT_FOUND = "404 page not found\n"

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
  k TEXT PRIMARY KEY,
  v TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS nodes (
  id INTEGER PRIMARY KEY,
  remark TEXT NOT NULL,
  addr TEXT NOT NULL,
  key TEXT NOT NULL UNIQUE,
  deleting INTEGER NOT NULL DEFAULT 0,
  first_seen INTEGER,
  last_seen INTEGER,
  src_ip TEXT,
  reported_v TEXT,
  state TEXT,
  err TEXT,
  agent TEXT,
  skew INTEGER
);
CREATE TABLE IF NOT EXISTS chains (
  id INTEGER PRIMARY KEY,
  remark TEXT NOT NULL,
  target_host TEXT NOT NULL,
  target_port INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS hops (
  id INTEGER PRIMARY KEY,
  chain_id INTEGER NOT NULL REFERENCES chains(id),
  seq INTEGER NOT NULL,
  node_id INTEGER NOT NULL REFERENCES nodes(id),
  port INTEGER NOT NULL,
  UNIQUE (node_id, port)
);
"""

HOST_RE = re.compile(
    r"^(?=.{1,253}$)([A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+[A-Za-z][A-Za-z0-9-]{0,62}$"
)


class UserError(Exception):
    pass


# ---------------------------------------------------------------- 数据库

def db():
    d = os.path.dirname(DB_PATH)
    if d:
        os.makedirs(d, mode=0o700, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    return conn


def setting(conn, k):
    row = conn.execute("SELECT v FROM settings WHERE k = ?", (k,)).fetchone()
    return row["v"] if row else None


def new_key():
    return secrets.token_hex(32)


# ---------------------------------------------------------------- 载荷加密
# AES-256-CTR + HMAC-SHA256，与节点端一样调用 openssl 命令行。
# 每条消息由 openssl 随机生成 salt，经 PBKDF2 派生独立的密钥和 IV；去掉固定的 "Salted__" 前缀后传输。
# 请求：salt(8) | 密文 | HMAC(salt | 密文)
# 响应：salt(8) | 密文 | HMAC(请求的 HMAC | salt | 密文)
# 请求里没有节点标识，服务端逐个用节点密钥校验 HMAC 来识别节点。

def derive(key):
    k = bytes.fromhex(key)
    return (hmac.new(k, b"ictl-enc", hashlib.sha256).hexdigest(),
            hmac.new(k, b"ictl-mac", hashlib.sha256).digest())


def aes(ek, data, decrypt=False):
    args = ["openssl", "enc", "-aes-256-ctr", "-pbkdf2", "-iter", "1", "-md", "sha256", "-pass", "env:ICTL_EK"]
    if decrypt:
        args.append("-d")
    return subprocess.run(
        args, input=data, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=True,
        env={**os.environ, "ICTL_EK": ek},
    ).stdout


def mac(mk, data):
    return hmac.new(mk, data, hashlib.sha256).digest()


def seal(key, req_tag, text):
    ek, mk = derive(key)
    msg = aes(ek, text.encode())[8:]
    return base64.b64encode(msg + mac(mk, req_tag + msg)).decode()


def get_node(conn, node_id):
    node = conn.execute("SELECT * FROM nodes WHERE id = ?", (node_id,)).fetchone()
    if not node:
        raise UserError(f"节点不存在：{node_id}")
    return node


def get_chain(conn, chain_id):
    chain = conn.execute("SELECT * FROM chains WHERE id = ?", (chain_id,)).fetchone()
    if not chain:
        raise UserError(f"链不存在：{chain_id}")
    return chain


# ---------------------------------------------------------------- 校验

def check_host(s):
    try:
        ipaddress.IPv4Address(s)
        return s
    except ValueError:
        pass
    if HOST_RE.match(s):
        return s.lower()
    raise UserError(f"不是有效的 IPv4 地址或域名：{s}")


def check_port(s):
    if re.fullmatch(r"[1-9][0-9]{0,4}", str(s)) and int(s) <= 65535:
        return int(s)
    raise UserError(f"端口必须是 1-65535 的整数：{s}")


def check_remark(s):
    s = s.strip()
    if not s or "\n" in s:
        raise UserError("备注不能为空，且不能换行")
    return s


def split_host_port(s):
    host, sep, port = s.rpartition(":")
    if not sep:
        raise UserError(f"格式应为 地址:端口：{s}")
    return check_host(host), check_port(port)


def parse_hops(conn, items, chain_id=None):
    """把 节点id:入站端口 列表解析为 [(node_id, port)] 并检查冲突。"""
    if not items:
        raise UserError("至少需要一跳")
    hops = []
    for item in items:
        nid, sep, port = item.partition(":")
        if not sep or not nid.isdigit():
            raise UserError(f"跳的格式应为 节点id:入站端口：{item}")
        node = get_node(conn, int(nid))
        if node["deleting"]:
            raise UserError(f"节点 {nid} 正在删除，不能使用")
        hops.append((node["id"], check_port(port)))

    seen = set()
    for h in hops:
        if h in seen:
            raise UserError(f"同一条链里重复使用了节点 {h[0]} 的入站端口 {h[1]}")
        seen.add(h)
        row = conn.execute(
            "SELECT chain_id FROM hops WHERE node_id = ? AND port = ? AND chain_id IS NOT ?",
            (h[0], h[1], chain_id),
        ).fetchone()
        if row:
            raise UserError(f"节点 {h[0]} 的入站端口 {h[1]} 已被链 {row['chain_id']} 使用")
    return hops


def save_hops(conn, chain_id, hops):
    conn.execute("DELETE FROM hops WHERE chain_id = ?", (chain_id,))
    for seq, (nid, port) in enumerate(hops):
        conn.execute(
            "INSERT INTO hops (chain_id, seq, node_id, port) VALUES (?, ?, ?, ?)",
            (chain_id, seq, nid, port),
        )


# ---------------------------------------------------------------- 规则展开

def node_rules(conn, node):
    """节点应有的规则行：规则id 协议 入站端口 目标地址 目标端口。"""
    if node["deleting"]:
        return []
    rows = conn.execute(
        """
        SELECT h.id, h.port, c.target_host, c.target_port, n2.addr AS next_addr, h2.port AS next_port
        FROM hops h
        JOIN chains c ON c.id = h.chain_id
        LEFT JOIN hops h2 ON h2.chain_id = h.chain_id AND h2.seq = h.seq + 1
        LEFT JOIN nodes n2 ON n2.id = h2.node_id
        WHERE h.node_id = ?
        ORDER BY h.id
        """,
        (node["id"],),
    )
    lines = []
    for r in rows:
        if r["next_port"] is None:
            host, port = r["target_host"], r["target_port"]
        else:
            host, port = r["next_addr"], r["next_port"]
        lines.append(f"{r['id']} both {r['port']} {host} {port}")
    return lines


def rules_version(lines):
    return hashlib.sha256("\n".join(lines).encode()).hexdigest()[:12]


def node_status(conn, node, now=None):
    now = now or int(time.time())
    if node["deleting"]:
        return "删除中"
    if not node["first_seen"]:
        return "未连接"
    if now - node["last_seen"] > OFFLINE_AFTER:
        return "离线"
    if node["skew"] is not None and abs(node["skew"]) > MAX_SKEW:
        return "时间不一致"
    if node["reported_v"] != rules_version(node_rules(conn, node)) or node["state"] == "init":
        return "同步中"
    return "已同步" if node["state"] == "ok" else "失败"


# ---------------------------------------------------------------- 同步接口

class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    sync_path = "/"

    # 不发送 Server 头，不暴露实现
    def send_response(self, code, message=None):
        self.send_response_only(code, message)
        self.send_header("Date", self.date_time_string())

    def log_message(self, fmt, *args):
        pass

    def reply(self, code, body, head_only=False):
        data = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if not head_only:
            self.wfile.write(data)

    def not_found(self):
        self.reply(404, NOT_FOUND)

    do_GET = do_PUT = do_DELETE = do_PATCH = do_OPTIONS = not_found

    def do_HEAD(self):
        self.reply(404, NOT_FOUND, head_only=True)

    def do_POST(self):
        try:
            self.sync()
        except Exception as e:  # 任何异常都不向外暴露细节
            print(f"sync error: {e!r}", file=sys.stderr)
            self.not_found()

    def source_ip(self):
        ip = self.client_address[0]
        xff = self.headers.get("X-Forwarded-For")
        if ip in ("127.0.0.1", "::1") and xff:
            ip = xff.split(",")[-1].strip()
        return ip

    def sync(self):
        if self.path != self.sync_path:
            return self.not_found()
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > MAX_BODY:
            return self.not_found()
        try:
            raw = base64.b64decode(self.rfile.read(length).strip(), validate=True)
        except ValueError:
            return self.not_found()
        if len(raw) < 8 + 32:
            return self.not_found()
        msg, req_tag = raw[:-32], raw[-32:]

        conn = db()
        try:
            for node in conn.execute("SELECT * FROM nodes").fetchall():
                ek, mk = derive(node["key"])
                if hmac.compare_digest(req_tag, mac(mk, msg)):
                    break
            else:
                return self.not_found()
            text = aes(ek, b"Salted__" + msg, decrypt=True).decode("utf-8", "replace")

            fields = {}
            for line in text.splitlines():
                k, sep, v = line.partition("=")
                if sep:
                    fields[k.strip()] = v.strip()
            state = fields.get("state", "")
            if state not in ("init", "ok", "error") or not fields.get("t", "").isdigit():
                return self.not_found()
            now = int(time.time())
            skew = int(fields["t"]) - now
            v = fields.get("v", "")

            with conn:
                # 时间相差过大：只记录时差，不处理请求（防重放）
                if abs(skew) > MAX_SKEW:
                    conn.execute(
                        """UPDATE nodes SET first_seen = COALESCE(first_seen, ?), last_seen = ?, src_ip = ?,
                           skew = ? WHERE id = ?""",
                        (now, now, self.source_ip(), skew, node["id"]),
                    )
                    return self.reply(200, seal(node["key"], req_tag, f"time={now}\n"))

                conn.execute(
                    """UPDATE nodes SET first_seen = COALESCE(first_seen, ?), last_seen = ?, src_ip = ?,
                       reported_v = ?, state = ?, err = ?, agent = ?, skew = ? WHERE id = ?""",
                    (now, now, self.source_ip(), v, state, fields.get("err", "")[:500],
                     fields.get("agent", "")[:32], skew, node["id"]),
                )
                lines = node_rules(conn, node)
                want = rules_version(lines)

                if node["deleting"] and v == want and state == "ok":
                    conn.execute("DELETE FROM nodes WHERE id = ?", (node["id"],))
                    out = "same\n"
                elif v == want and state != "init":
                    out = "same\n"
                else:
                    out = "".join(f"{x}\n" for x in [f"v={want}"] + lines)
            self.reply(200, seal(node["key"], req_tag, out))
        finally:
            conn.close()


def cmd_serve(args):
    conn = db()
    path = setting(conn, "path")
    conn.close()
    if not path:
        raise UserError("尚未初始化，请先执行 init")
    if not shutil.which("openssl"):
        raise UserError("缺少依赖：openssl")
    host, _, port = args.listen.rpartition(":")
    Handler.sync_path = path
    server = http.server.ThreadingHTTPServer((host or "127.0.0.1", int(port)), Handler)
    print(f"ictl-server {VERSION} 监听 {args.listen}，同步路径 {path}", flush=True)
    server.serve_forever()


# ---------------------------------------------------------------- 命令行输出

def width(s):
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in s)


def table(headers, rows):
    rows = [[str(c) for c in r] for r in rows]
    ws = [max(width(x) for x in col) for col in zip(headers, *rows)]
    for r in [headers] + rows:
        print("  ".join(c + " " * (w - width(c)) for c, w in zip(r, ws)).rstrip())


def ago(ts, now):
    if not ts:
        return "-"
    d = now - ts
    if d < 60:
        return f"{d} 秒前"
    if d < 3600:
        return f"{d // 60} 分钟前"
    if d < 86400:
        return f"{d // 3600} 小时前"
    return f"{d // 86400} 天前"


def print_credentials(conn, key):
    url = (setting(conn, "url") or "").rstrip("/") + (setting(conn, "path") or "")
    print(f"  同步地址：{url}")
    print(f"  密钥：    {key}")
    print("  密钥只显示这一次，请填写到节点端（ictl 菜单 1）。")


# ---------------------------------------------------------------- 命令：初始化

def cmd_init(args):
    if not re.match(r"^https?://[^/\s]+$", args.url):
        raise UserError("地址格式应为 https://域名（不带路径）")
    path = args.path or "/" + secrets.token_hex(12)
    if not path.startswith("/") or not re.fullmatch(r"/[A-Za-z0-9._~/-]*", path):
        raise UserError(f"路径无效：{path}")
    conn = db()
    with conn:
        existed = setting(conn, "path")
        conn.execute("INSERT OR REPLACE INTO settings VALUES ('url', ?)", (args.url,))
        conn.execute("INSERT OR REPLACE INTO settings VALUES ('path', ?)", (path,))
    print(f"同步地址：{args.url}{path}")
    if existed and existed != path:
        print("同步地址已变更，所有节点需要重新填写地址，并重启 serve。")


# ---------------------------------------------------------------- 命令：节点

def cmd_node_add(args):
    conn = db()
    key = new_key()
    with conn:
        cur = conn.execute(
            "INSERT INTO nodes (remark, addr, key) VALUES (?, ?, ?)",
            (check_remark(args.remark), check_host(args.addr), key),
        )
    print(f"已添加节点 {cur.lastrowid}")
    print_credentials(conn, key)


def cmd_node_list(args):
    conn = db()
    now = int(time.time())
    rows = []
    for n in conn.execute("SELECT * FROM nodes ORDER BY id"):
        rows.append([
            n["id"], n["remark"], n["addr"], node_status(conn, n, now), ago(n["last_seen"], now),
            n["src_ip"] or "-", "-" if n["skew"] is None else f"{n['skew']:+d}s",
            n["agent"] or "-", n["err"] or "",
        ])
    if not rows:
        print("没有节点。")
        return
    table(["ID", "备注", "入站地址", "状态", "最后通信", "来源IP", "时差", "版本", "错误"], rows)


def upstream_nodes(conn, node_id):
    """规则里以该节点为下一跳的节点 id。"""
    return [r[0] for r in conn.execute(
        """SELECT DISTINCT h.node_id FROM hops h
           JOIN hops h2 ON h2.chain_id = h.chain_id AND h2.seq = h.seq + 1
           WHERE h2.node_id = ? ORDER BY h.node_id""",
        (node_id,),
    )]


def cmd_node_edit(args):
    conn = db()
    node = get_node(conn, args.id)
    remark = check_remark(args.remark) if args.remark is not None else node["remark"]
    addr = check_host(args.addr) if args.addr is not None else node["addr"]
    key = None
    with conn:
        conn.execute("UPDATE nodes SET remark = ?, addr = ? WHERE id = ?", (remark, addr, node["id"]))
        if addr != node["addr"]:
            key = new_key()
            conn.execute("UPDATE nodes SET key = ? WHERE id = ?", (key, node["id"]))
    print(f"已修改节点 {node['id']}")
    if key:
        print("入站地址已变更，旧密钥已失效：")
        print_credentials(conn, key)
        ups = upstream_nodes(conn, node["id"])
        if ups:
            print(f"以下节点的规则会自动更新：{', '.join(map(str, ups))}")


def cmd_node_rekey(args):
    conn = db()
    node = get_node(conn, args.id)
    key = new_key()
    with conn:
        conn.execute("UPDATE nodes SET key = ? WHERE id = ?", (key, node["id"]))
    print(f"节点 {node['id']} 已重新生成密钥，旧密钥已失效：")
    print_credentials(conn, key)


def cmd_node_del(args):
    conn = db()
    node = get_node(conn, args.id)
    used = [r[0] for r in conn.execute(
        "SELECT DISTINCT chain_id FROM hops WHERE node_id = ? ORDER BY chain_id", (node["id"],))]
    if used:
        raise UserError(f"节点仍被链 {', '.join(map(str, used))} 使用，请先删除或修改这些链")

    if args.force or not node["first_seen"]:
        with conn:
            conn.execute("DELETE FROM nodes WHERE id = ?", (node["id"],))
        print(f"已删除节点 {node['id']}" + ("（未清理节点上的规则）" if node["first_seen"] else ""))
        return

    with conn:
        conn.execute("UPDATE nodes SET deleting = 1 WHERE id = ?", (node["id"],))
    print(f"已向节点 {node['id']} 下发空配置，等待节点确认（最多 {args.wait} 秒）...")
    deadline = time.time() + args.wait
    while time.time() < deadline:
        if not conn.execute("SELECT 1 FROM nodes WHERE id = ?", (node["id"],)).fetchone():
            print(f"节点已确认清空规则，删除成功。")
            return
        time.sleep(1)
    print("等待超时：节点确认后会自动删除。节点已失联时可使用 --force 直接删除。")


# ---------------------------------------------------------------- 命令：链

def chain_path(conn, chain):
    parts = []
    for h in conn.execute(
        """SELECT h.port, n.id, n.remark FROM hops h JOIN nodes n ON n.id = h.node_id
           WHERE h.chain_id = ? ORDER BY h.seq""",
        (chain["id"],),
    ):
        parts.append(f"{h['remark']}({h['id']}):{h['port']}")
    parts.append(f"{chain['target_host']}:{chain['target_port']}")
    return " → ".join(parts)


def cmd_chain_add(args):
    conn = db()
    remark = check_remark(args.remark)
    host, port = split_host_port(args.target)
    with conn:
        hops = parse_hops(conn, args.hops)
        cur = conn.execute(
            "INSERT INTO chains (remark, target_host, target_port) VALUES (?, ?, ?)", (remark, host, port))
        save_hops(conn, cur.lastrowid, hops)
    print(f"已添加链 {cur.lastrowid}：{chain_path(conn, get_chain(conn, cur.lastrowid))}")


def cmd_chain_list(args):
    conn = db()
    rows = [[c["id"], c["remark"], chain_path(conn, c)]
            for c in conn.execute("SELECT * FROM chains ORDER BY id")]
    if not rows:
        print("没有链。")
        return
    table(["ID", "备注", "路径"], rows)


def cmd_chain_edit(args):
    conn = db()
    chain = get_chain(conn, args.id)
    remark = check_remark(args.remark) if args.remark is not None else chain["remark"]
    if args.target is not None:
        host, port = split_host_port(args.target)
    else:
        host, port = chain["target_host"], chain["target_port"]
    with conn:
        if args.hops is not None:
            save_hops(conn, chain["id"], parse_hops(conn, args.hops, chain["id"]))
        conn.execute(
            "UPDATE chains SET remark = ?, target_host = ?, target_port = ? WHERE id = ?",
            (remark, host, port, chain["id"]))
    print(f"已修改链 {chain['id']}：{chain_path(conn, get_chain(conn, chain['id']))}")


def cmd_chain_del(args):
    conn = db()
    chain = get_chain(conn, args.id)
    with conn:
        conn.execute("DELETE FROM hops WHERE chain_id = ?", (chain["id"],))
        conn.execute("DELETE FROM chains WHERE id = ?", (chain["id"],))
    print(f"已删除链 {chain['id']}")


# ---------------------------------------------------------------- 入口

def main():
    p = argparse.ArgumentParser(prog="ictl-server", description=f"ictl 服务端 {VERSION}")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("init", help="设置对外地址和同步路径")
    s.add_argument("url", help="对外地址，如 https://example.com")
    s.add_argument("--path", help="同步路径，默认随机生成")
    s.set_defaults(func=cmd_init)

    s = sub.add_parser("serve", help="运行同步接口")
    s.add_argument("--listen", default="127.0.0.1:8730", help="监听地址，默认 127.0.0.1:8730")
    s.set_defaults(func=cmd_serve)

    node = sub.add_parser("node", help="节点管理").add_subparsers(dest="sub", required=True)
    s = node.add_parser("add", help="添加节点")
    s.add_argument("remark")
    s.add_argument("addr", help="入站地址（IPv4 或域名）")
    s.set_defaults(func=cmd_node_add)
    s = node.add_parser("list", help="列出节点及状态")
    s.set_defaults(func=cmd_node_list)
    s = node.add_parser("edit", help="修改节点（修改入站地址会重新生成密钥）")
    s.add_argument("id", type=int)
    s.add_argument("--remark")
    s.add_argument("--addr")
    s.set_defaults(func=cmd_node_edit)
    s = node.add_parser("rekey", help="重新生成密钥")
    s.add_argument("id", type=int)
    s.set_defaults(func=cmd_node_rekey)
    s = node.add_parser("del", help="删除节点（先清空节点规则，确认后删除）")
    s.add_argument("id", type=int)
    s.add_argument("--force", action="store_true", help="不等节点确认，直接删除记录")
    s.add_argument("--wait", type=int, default=60, help="等待节点确认的秒数，默认 60")
    s.set_defaults(func=cmd_node_del)

    chain = sub.add_parser("chain", help="链管理").add_subparsers(dest="sub", required=True)
    s = chain.add_parser("add", help="添加链")
    s.add_argument("remark")
    s.add_argument("target", help="最终目标 地址:端口")
    s.add_argument("hops", nargs="+", help="依次为 节点id:入站端口")
    s.set_defaults(func=cmd_chain_add)
    s = chain.add_parser("list", help="列出链")
    s.set_defaults(func=cmd_chain_list)
    s = chain.add_parser("edit", help="修改链")
    s.add_argument("id", type=int)
    s.add_argument("--remark")
    s.add_argument("--target", help="最终目标 地址:端口")
    s.add_argument("--hops", nargs="+", help="依次为 节点id:入站端口（整体替换）")
    s.set_defaults(func=cmd_chain_edit)
    s = chain.add_parser("del", help="删除链")
    s.add_argument("id", type=int)
    s.set_defaults(func=cmd_chain_del)

    args = p.parse_args()
    os.umask(0o077)  # 数据库保存密钥原文，只允许属主读写
    try:
        args.func(args)
    except UserError as e:
        print(f"错误：{e}", file=sys.stderr)
        sys.exit(1)
    except sqlite3.IntegrityError as e:
        print(f"错误：数据冲突（{e}）", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
