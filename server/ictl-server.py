#!/usr/bin/env python3
"""ictl 服务端：节点同步接口 + Web 管理面板。

数据库路径默认 /var/lib/ictl-server/ictl.db，可用环境变量 ICTL_DB 覆盖。
"""

import argparse
import base64
import hashlib
import hmac
import http.cookies
import http.server
import ipaddress
import json
import os
import re
import secrets
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
from urllib.parse import parse_qs

VERSION = "0.1.3"
REPO = "moran1024a/iptables-control"
AGENT_URL = f"https://github.com/{REPO}/releases/download/v{VERSION}/ictl.sh"
DB_PATH = os.environ.get("ICTL_DB", "/var/lib/ictl-server/ictl.db")
WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")

OFFLINE_AFTER = 30          # 超过该秒数没有上报视为离线
MAX_SKEW = 60               # 节点时间允许的最大偏差（秒）
MAX_BODY = 8192             # 同步请求体上限
MAX_API_BODY = 65536        # 面板请求体上限
WINDOW = 30                 # 状态条时间窗口（秒）
BAR_WINDOWS = 60            # 状态条显示的窗口数
RETENTION = 7 * 86400       # 状态窗口与事件保留时长
EVENTS_SHOWN = 50
SESSION_TTL = 7 * 86400
SESSION_COOKIE = "ictl_session"
LOGIN_MAX_FAILS = 5
LOGIN_LOCK = 300
DEFAULT_USER = "admin"
DEFAULT_PASS = "admin_pass"
NOT_FOUND = "404 page not found\n"
STATIC = {
    "index.html": "text/html; charset=utf-8",
    "app.js": "text/javascript; charset=utf-8",
    "style.css": "text/css; charset=utf-8",
}
HEALTH = {"ok": "正常", "error": "故障", "skew": "时间不一致"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
  k TEXT PRIMARY KEY,
  v TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
  token_hash TEXT PRIMARY KEY,
  expires INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS nodes (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL,
  note TEXT NOT NULL DEFAULT '',
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
  skew INTEGER,
  health TEXT
);
CREATE TABLE IF NOT EXISTS chains (
  id INTEGER PRIMARY KEY,
  remark TEXT NOT NULL,
  target_host TEXT NOT NULL,
  target_port INTEGER NOT NULL,
  deleting INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS hops (
  id INTEGER PRIMARY KEY,
  chain_id INTEGER NOT NULL REFERENCES chains(id),
  seq INTEGER NOT NULL,
  node_id INTEGER NOT NULL REFERENCES nodes(id),
  port INTEGER NOT NULL,
  UNIQUE (node_id, port)
);
CREATE TABLE IF NOT EXISTS chain_pending (
  chain_id INTEGER NOT NULL,
  node_id INTEGER NOT NULL,
  PRIMARY KEY (chain_id, node_id)
);
CREATE TABLE IF NOT EXISTS windows (
  node_id INTEGER NOT NULL,
  win INTEGER NOT NULL,
  level INTEGER NOT NULL,
  PRIMARY KEY (node_id, win)
);
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY,
  node_id INTEGER NOT NULL,
  ts INTEGER NOT NULL,
  text TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS events_node ON events (node_id, ts);
"""

HOST_RE = re.compile(
    r"^(?=.{1,253}$)([A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+[A-Za-z][A-Za-z0-9-]{0,62}$"
)


class UserError(Exception):
    def __init__(self, msg, field=None, status=400):
        super().__init__(msg)
        self.field = field
        self.status = status


# ---------------------------------------------------------------- 数据库

def db():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    d = os.path.dirname(DB_PATH)
    if d:
        os.makedirs(d, mode=0o700, exist_ok=True)
    conn = db()
    conn.execute("PRAGMA journal_mode = WAL")
    conn.executescript(SCHEMA)
    # 旧版本升级
    cols = lambda t: [r["name"] for r in conn.execute(f"PRAGMA table_info({t})")]
    if "deleting" not in cols("chains"):                   # 0.1.0：转发删除确认
        conn.execute("ALTER TABLE chains ADD COLUMN deleting INTEGER NOT NULL DEFAULT 0")
    if "remark" in cols("nodes"):                          # 0.1.1：节点「备注」改为「名称」，新增「备注」
        conn.execute("ALTER TABLE nodes RENAME COLUMN remark TO name")
    if "note" not in cols("nodes"):
        conn.execute("ALTER TABLE nodes ADD COLUMN note TEXT NOT NULL DEFAULT ''")
    with conn:
        if not setting(conn, "sync_path"):
            set_setting(conn, "sync_path", "/" + secrets.token_hex(12))
        if not setting(conn, "panel_path"):
            set_setting(conn, "panel_path", "/" + secrets.token_hex(8))
        if not setting(conn, "admin_user"):
            reset_admin(conn)
    return conn


def setting(conn, k, default=None):
    row = conn.execute("SELECT v FROM settings WHERE k = ?", (k,)).fetchone()
    return row["v"] if row else default


def set_setting(conn, k, v):
    conn.execute("INSERT OR REPLACE INTO settings VALUES (?, ?)", (k, v))


def reset_admin(conn):
    set_setting(conn, "admin_user", DEFAULT_USER)
    set_setting(conn, "admin_hash", hash_password(DEFAULT_PASS))
    set_setting(conn, "must_change", "1")
    conn.execute("DELETE FROM sessions")


def get_node(conn, node_id):
    node = conn.execute("SELECT * FROM nodes WHERE id = ?", (node_id,)).fetchone()
    if not node:
        raise UserError("节点不存在", status=404)
    return node


def get_chain(conn, chain_id):
    chain = conn.execute("SELECT * FROM chains WHERE id = ?", (chain_id,)).fetchone()
    if not chain:
        raise UserError("转发不存在", status=404)
    return chain


def delete_node(conn, node_id):
    conn.execute("DELETE FROM nodes WHERE id = ?", (node_id,))
    conn.execute("DELETE FROM windows WHERE node_id = ?", (node_id,))
    conn.execute("DELETE FROM events WHERE node_id = ?", (node_id,))
    conn.execute("DELETE FROM chain_pending WHERE node_id = ?", (node_id,))
    finish_deleted_chains(conn)


def delete_chain(conn, chain_id):
    conn.execute("DELETE FROM hops WHERE chain_id = ?", (chain_id,))
    conn.execute("DELETE FROM chain_pending WHERE chain_id = ?", (chain_id,))
    conn.execute("DELETE FROM chains WHERE id = ?", (chain_id,))


def mark_pending(conn, chain_id, node_ids):
    """记录本次改动需要哪些节点确认。"""
    for nid in set(node_ids):
        conn.execute("INSERT OR IGNORE INTO chain_pending VALUES (?, ?)", (chain_id, nid))


def finish_deleted_chains(conn):
    """删除中的转发在所有相关节点确认后真正删除。"""
    for r in conn.execute(
            """SELECT id FROM chains WHERE deleting = 1
               AND id NOT IN (SELECT chain_id FROM chain_pending)""").fetchall():
        delete_chain(conn, r["id"])


def add_event(conn, node_id, text, now=None):
    conn.execute("INSERT INTO events (node_id, ts, text) VALUES (?, ?, ?)",
                 (node_id, now or int(time.time()), text))


def cleanup(conn):
    now = int(time.time())
    with conn:
        conn.execute("DELETE FROM windows WHERE win < ?", ((now - RETENTION) // WINDOW,))
        conn.execute("DELETE FROM events WHERE ts < ?", (now - RETENTION,))
        conn.execute("DELETE FROM sessions WHERE expires < ?", (now,))


# ---------------------------------------------------------------- 密码

def hash_password(password, salt=None):
    salt = salt or secrets.token_bytes(16)
    h = hashlib.scrypt(password.encode(), salt=salt, n=2 ** 14, r=8, p=1, dklen=32)
    return f"scrypt${salt.hex()}${h.hex()}"


def verify_password(password, stored):
    try:
        _, salt, h = stored.split("$")
        return hmac.compare_digest(hash_password(password, bytes.fromhex(salt)), stored)
    except ValueError:
        return False


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


# ---------------------------------------------------------------- 校验

def check_host(s, field):
    s = str(s or "").strip()
    try:
        ipaddress.IPv4Address(s)
        return s
    except ValueError:
        pass
    if HOST_RE.match(s):
        return s.lower()
    raise UserError("请填写有效的 IPv4 地址或域名", field)


def check_port(s, field):
    s = str(s or "").strip()
    if re.fullmatch(r"[1-9][0-9]{0,4}", s) and int(s) <= 65535:
        return int(s)
    raise UserError("端口必须是 1-65535 的整数", field)


def check_remark(s, field="remark", label="别名"):
    s = str(s or "").strip()
    if not s:
        raise UserError(f"{label}不能为空", field)
    if len(s) > 64 or "\n" in s:
        raise UserError(f"{label}最长 64 个字符，且不能换行", field)
    return s


def check_note(s):
    s = str(s or "").strip().replace("\r\n", "\n")
    if len(s) > 500:
        raise UserError("备注最长 500 个字符", "note")
    return s


def parse_hops(conn, hops, chain_id=None):
    """把 [{node_id, port}] 解析为 [(node_id, port)] 并检查端口冲突。"""
    if not isinstance(hops, list) or not hops:
        raise UserError("至少需要一跳", "hops")
    result = []
    for i, h in enumerate(hops, 1):
        nid = h.get("node_id") if isinstance(h, dict) else None
        node = conn.execute("SELECT * FROM nodes WHERE id = ?", (nid,)).fetchone() if isinstance(nid, int) else None
        if not node:
            raise UserError(f"第 {i} 跳：请选择节点", "hops")
        if node["deleting"]:
            raise UserError(f"第 {i} 跳：节点 {node['name']} 正在删除，不能使用", "hops")
        try:
            port = check_port(h.get("port"), "hops")
        except UserError:
            raise UserError(f"第 {i} 跳：端口必须是 1-65535 的整数", "hops")
        result.append((node, port))

    seen = set()
    for i, (node, port) in enumerate(result, 1):
        if (node["id"], port) in seen:
            raise UserError(f"第 {i} 跳：重复使用了节点 {node['name']} 的端口 {port}", "hops")
        seen.add((node["id"], port))
        row = conn.execute(
            """SELECT c.remark || CASE c.deleting WHEN 1 THEN '（删除中）' ELSE '' END AS remark
               FROM hops h JOIN chains c ON c.id = h.chain_id
               WHERE h.node_id = ? AND h.port = ? AND h.chain_id IS NOT ?""",
            (node["id"], port, chain_id),
        ).fetchone()
        if row:
            raise UserError(f"第 {i} 跳：节点 {node['name']} 的端口 {port} 已被转发「{row['remark']}」使用", "hops")
    return [(node["id"], port) for node, port in result]


def save_hops(conn, chain_id, hops):
    conn.execute("DELETE FROM hops WHERE chain_id = ?", (chain_id,))
    for seq, (nid, port) in enumerate(hops):
        conn.execute(
            "INSERT INTO hops (chain_id, seq, node_id, port) VALUES (?, ?, ?, ?)",
            (chain_id, seq, nid, port),
        )


# ---------------------------------------------------------------- 规则展开与状态

def node_rule_rows(conn, node):
    """节点应有的规则：非最后一跳指向下一跳节点，最后一跳指向链的目标。"""
    if node["deleting"]:
        return []
    rows = conn.execute(
        """
        SELECT h.id, h.port, c.id AS chain_id, c.remark AS chain_remark, c.target_host, c.target_port,
               n2.addr AS next_addr, h2.port AS next_port
        FROM hops h
        JOIN chains c ON c.id = h.chain_id
        LEFT JOIN hops h2 ON h2.chain_id = h.chain_id AND h2.seq = h.seq + 1
        LEFT JOIN nodes n2 ON n2.id = h2.node_id
        WHERE h.node_id = ? AND c.deleting = 0
        ORDER BY h.id
        """,
        (node["id"],),
    )
    out = []
    for r in rows:
        if r["next_port"] is None:
            host, port = r["target_host"], r["target_port"]
        else:
            host, port = r["next_addr"], r["next_port"]
        out.append({"id": r["id"], "port": r["port"], "host": host, "tport": port,
                    "chain_id": r["chain_id"], "chain_remark": r["chain_remark"]})
    return out


def node_rules(conn, node):
    """下发给节点的规则行：规则id 协议 入站端口 目标地址 目标端口。"""
    return [f"{r['id']} both {r['port']} {r['host']} {r['tport']}" for r in node_rule_rows(conn, node)]


def rules_version(lines):
    return hashlib.sha256("\n".join(lines).encode()).hexdigest()[:12]


def node_status(conn, node, now):
    """new / offline / skew / syncing / synced / error / deleting"""
    if node["deleting"]:
        return "deleting"
    if not node["first_seen"]:
        return "new"
    if now - node["last_seen"] > OFFLINE_AFTER:
        return "offline"
    if node["skew"] is not None and abs(node["skew"]) > MAX_SKEW:
        return "skew"
    if node["reported_v"] != rules_version(node_rules(conn, node)) or node["state"] == "init":
        return "syncing"
    return "synced" if node["state"] == "ok" else "error"


def node_bar(conn, node, now):
    """最近 BAR_WINDOWS 个时间窗口的状态：ok / fault / lost / none。"""
    cur = now // WINDOW
    start = cur - BAR_WINDOWS + 1
    levels = {r[0]: r[1] for r in conn.execute(
        "SELECT win, level FROM windows WHERE node_id = ? AND win >= ?", (node["id"], start))}
    first = node["first_seen"]
    bar = []
    for w in range(start, cur + 1):
        if w in levels:
            bar.append("ok" if levels[w] == 2 else "fault")
        elif not first or (w + 1) * WINDOW <= first or w == cur:
            bar.append("none")
        else:
            bar.append("lost")
    return {"start": start * WINDOW, "window": WINDOW, "levels": bar}


def duration(sec):
    if sec < 60:
        return f"{sec} 秒"
    if sec < 3600:
        return f"{sec // 60} 分 {sec % 60} 秒"
    if sec < 86400:
        return f"{sec // 3600} 小时 {sec % 3600 // 60} 分"
    return f"{sec // 86400} 天 {sec % 86400 // 3600} 小时"


def record_report(conn, node, now, health, err, skew):
    """记录一次上报：状态窗口 + 状态变化事件。"""
    if not node["first_seen"]:
        add_event(conn, node["id"], "首次连接", now)
    elif now - node["last_seen"] > OFFLINE_AFTER:
        add_event(conn, node["id"], f"失联 {duration(now - node['last_seen'])} 后恢复", now)

    prev = node["health"] or "ok"
    if health != prev:
        text = f"{HEALTH[prev]} → {HEALTH[health]}"
        if health == "error" and err:
            text += f"：{err}"
        elif health == "skew":
            text += f"（{skew:+d} 秒）"
        add_event(conn, node["id"], text, now)

    conn.execute(
        """INSERT INTO windows VALUES (?, ?, ?)
           ON CONFLICT (node_id, win) DO UPDATE SET level = MIN(level, excluded.level)""",
        (node["id"], now // WINDOW, 2 if health == "ok" else 1),
    )


def upstream_nodes(conn, node_id):
    """以该节点为下一跳的节点名称。"""
    return [r[0] for r in conn.execute(
        """SELECT DISTINCT n.name FROM hops h
           JOIN hops h2 ON h2.chain_id = h.chain_id AND h2.seq = h.seq + 1
           JOIN nodes n ON n.id = h.node_id
           WHERE h2.node_id = ? ORDER BY n.id""",
        (node_id,),
    )]


# ---------------------------------------------------------------- HTTP

LOGIN_FAILS = {}            # ip → [失败次数, 锁定到期时间]
LOGIN_FAILS_LOCK = threading.Lock()


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    sync_path = "/"
    panel_path = "/"

    # 不发送 Server 头，不暴露实现
    def send_response(self, code, message=None):
        self.send_response_only(code, message)
        self.send_header("Date", self.date_time_string())

    def log_message(self, fmt, *args):
        pass

    def reply(self, code, body, ctype="text/plain; charset=utf-8", headers=()):
        data = body if isinstance(body, bytes) else body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        for k, v in headers:
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def not_found(self):
        self.reply(404, NOT_FOUND)

    def send_json(self, code, obj):
        headers = [("Cache-Control", "no-store")] + [("Set-Cookie", c) for c in self.cookies]
        self.reply(code, json.dumps(obj, ensure_ascii=False), "application/json; charset=utf-8", headers)

    def do_GET(self):
        self.route("GET")

    def do_POST(self):
        self.route("POST")

    def do_PATCH(self):
        self.route("PATCH")

    def do_DELETE(self):
        self.route("DELETE")

    do_PUT = do_OPTIONS = do_HEAD = not_found

    def route(self, method):
        self.cookies = []
        try:
            path, _, query = self.path.partition("?")
            if path == self.sync_path:
                return self.sync() if method == "POST" else self.not_found()
            if path == self.panel_path and method == "GET":
                return self.reply(302, "", headers=[("Location", self.panel_path + "/")])
            prefix = self.panel_path + "/"
            if path.startswith(prefix):
                sub = path[len(prefix):]
                if sub.startswith("api/"):
                    return self.api(method, sub[4:], parse_qs(query))
                if method == "GET":
                    return self.static(sub or "index.html")
            self.not_found()
        except Exception as e:  # 任何异常都不向外暴露细节
            print(f"{method} error: {e!r}", file=sys.stderr)
            try:
                self.not_found()
            except Exception:
                pass

    def source_ip(self):
        """真实来源 IP。直连来源为本机或内网（反向代理、Docker 网桥）时才信任 X-Forwarded-For，
        公网客户端直连时自带的该请求头会被忽略，无法伪造。"""
        ip = self.client_address[0]
        xff = self.headers.get("X-Forwarded-For")
        if xff:
            peer = ipaddress.ip_address(ip)
            if peer.is_private or peer.is_loopback:
                ip = xff.split(",")[-1].strip()
        return ip

    def static(self, name):
        if name not in STATIC:
            return self.not_found()
        with open(os.path.join(WEB_DIR, name), "rb") as f:
            data = f.read()
        self.reply(200, data, STATIC[name], [("Cache-Control", "no-cache")])

    # ------------------------------------------------------------ 同步接口

    def sync(self):
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
            err = fields.get("err", "")[:500]
            health = "skew" if abs(skew) > MAX_SKEW else ("error" if state == "error" else "ok")

            with conn:
                record_report(conn, node, now, health, err, skew)

                # 时间相差过大：只记录时差，不处理请求（防重放）
                if health == "skew":
                    conn.execute(
                        """UPDATE nodes SET first_seen = COALESCE(first_seen, ?), last_seen = ?, src_ip = ?,
                           skew = ?, health = ? WHERE id = ?""",
                        (now, now, self.source_ip(), skew, health, node["id"]),
                    )
                    return self.reply(200, seal(node["key"], req_tag, f"time={now}\n"))

                conn.execute(
                    """UPDATE nodes SET first_seen = COALESCE(first_seen, ?), last_seen = ?, src_ip = ?,
                       reported_v = ?, state = ?, err = ?, agent = ?, skew = ?, health = ? WHERE id = ?""",
                    (now, now, self.source_ip(), v, state, err, fields.get("agent", "")[:32], skew, health,
                     node["id"]),
                )
                lines = node_rules(conn, node)
                want = rules_version(lines)

                if v == want and state != "init":
                    conn.execute("DELETE FROM chain_pending WHERE node_id = ?", (node["id"],))
                    finish_deleted_chains(conn)

                if node["deleting"] and v == want and state == "ok":
                    delete_node(conn, node["id"])
                    out = "same\n"
                elif v == want and state != "init":
                    out = "same\n"
                else:
                    out = "".join(f"{x}\n" for x in [f"v={want}"] + lines)
            self.reply(200, seal(node["key"], req_tag, out))
        finally:
            conn.close()

    # ------------------------------------------------------------ 面板接口

    def api(self, method, route, query):
        conn = db()
        try:
            # 修改类请求必须是 JSON，配合 SameSite=Strict 防跨站请求
            if method != "GET" and not self.headers.get("Content-Type", "").startswith("application/json"):
                return self.send_json(400, {"error": "请求格式错误"})
            body = self.read_json() if method != "GET" else {}

            if route == "login" and method == "POST":
                return self.api_login(conn, body)
            session = self.auth(conn)
            if not session:
                return self.send_json(401, {"error": "未登录"})
            if route == "logout" and method == "POST":
                with conn:
                    conn.execute("DELETE FROM sessions WHERE token_hash = ?", (session,))
                self.cookies.append(self.session_cookie("", 0))
                return self.send_json(200, {})
            if route == "me" and method == "GET":
                return self.send_json(200, {"username": setting(conn, "admin_user"),
                                            "must_change": setting(conn, "must_change") == "1"})
            if route == "account" and method == "POST":
                return self.api_account(conn, body, session)
            if setting(conn, "must_change") == "1":
                return self.send_json(403, {"error": "请先修改默认账号和密码", "must_change": True})

            parts = route.split("/")
            handler = None
            if parts == ["nodes"]:
                handler = {"GET": self.api_nodes, "POST": self.api_node_add}.get(method)
            elif len(parts) == 2 and parts[0] == "nodes" and parts[1].isdigit():
                handler = {"GET": self.api_node_get, "PATCH": self.api_node_edit,
                           "DELETE": self.api_node_del}.get(method)
            elif len(parts) == 3 and parts[0] == "nodes" and parts[1].isdigit() and parts[2] == "rekey":
                handler = {"POST": self.api_node_rekey}.get(method)
            elif parts == ["chains"]:
                handler = {"GET": self.api_chains, "POST": self.api_chain_save}.get(method)
            elif len(parts) == 2 and parts[0] == "chains" and parts[1].isdigit():
                handler = {"PATCH": self.api_chain_save, "DELETE": self.api_chain_del}.get(method)
            elif parts == ["settings"]:
                handler = {"GET": self.api_settings, "PATCH": self.api_settings_save}.get(method)
            if not handler:
                return self.send_json(404, {"error": "接口不存在"})

            args = [int(parts[1])] if len(parts) > 1 else []
            self.query = query
            self.send_json(200, handler(conn, body, *args))
        except UserError as e:
            self.send_json(e.status, {"error": str(e), "field": e.field})
        finally:
            conn.close()

    def read_json(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_API_BODY:
            raise UserError("请求过大")
        if length == 0:
            return {}
        try:
            body = json.loads(self.rfile.read(length))
        except ValueError:
            raise UserError("请求格式错误")
        if not isinstance(body, dict):
            raise UserError("请求格式错误")
        return body

    def session_cookie(self, token, max_age):
        c = f"{SESSION_COOKIE}={token}; Path={self.panel_path}/; Max-Age={max_age}; HttpOnly; SameSite=Strict"
        if self.headers.get("X-Forwarded-Proto") == "https":
            c += "; Secure"
        return c

    def auth(self, conn):
        """返回当前会话的令牌哈希；剩余有效期不足 6 天时自动续期。"""
        try:
            cookie = http.cookies.SimpleCookie(self.headers.get("Cookie", ""))
        except http.cookies.CookieError:
            return None
        if SESSION_COOKIE not in cookie:
            return None
        token = cookie[SESSION_COOKIE].value
        th = hashlib.sha256(token.encode()).hexdigest()
        now = int(time.time())
        row = conn.execute("SELECT expires FROM sessions WHERE token_hash = ?", (th,)).fetchone()
        if not row or row["expires"] < now:
            return None
        if row["expires"] - now < SESSION_TTL - 86400:
            with conn:
                conn.execute("UPDATE sessions SET expires = ? WHERE token_hash = ?", (now + SESSION_TTL, th))
            self.cookies.append(self.session_cookie(token, SESSION_TTL))
        return th

    def api_login(self, conn, body):
        ip = self.source_ip()
        now = time.time()
        with LOGIN_FAILS_LOCK:
            fails = LOGIN_FAILS.get(ip)
            if fails and fails[1] > now:
                return self.send_json(429, {"error": f"尝试次数过多，请 {int(fails[1] - now) + 1} 秒后再试"})

        user_ok = hmac.compare_digest(str(body.get("username", "")), setting(conn, "admin_user"))
        pass_ok = verify_password(str(body.get("password", "")), setting(conn, "admin_hash"))
        if not (user_ok and pass_ok):
            with LOGIN_FAILS_LOCK:
                fails = LOGIN_FAILS.setdefault(ip, [0, 0])
                fails[0] += 1
                if fails[0] >= LOGIN_MAX_FAILS:
                    LOGIN_FAILS[ip] = [0, now + LOGIN_LOCK]
            return self.send_json(401, {"error": "用户名或密码错误"})

        with LOGIN_FAILS_LOCK:
            LOGIN_FAILS.pop(ip, None)
        token = secrets.token_urlsafe(32)
        with conn:
            conn.execute("INSERT INTO sessions VALUES (?, ?)",
                         (hashlib.sha256(token.encode()).hexdigest(), int(now) + SESSION_TTL))
        self.cookies.append(self.session_cookie(token, SESSION_TTL))
        self.send_json(200, {"must_change": setting(conn, "must_change") == "1"})

    def api_account(self, conn, body, session):
        if not verify_password(str(body.get("current_password", "")), setting(conn, "admin_hash")):
            raise UserError("当前密码错误", "current_password")
        username = str(body.get("username", "")).strip()
        password = str(body.get("password", ""))
        must_change = setting(conn, "must_change") == "1"
        if not re.fullmatch(r"[A-Za-z0-9_.@-]{1,32}", username):
            raise UserError("用户名为 1-32 位字母、数字或 _ . @ -", "username")
        # 非首次修改时，新密码留空表示不修改
        if (password or must_change) and not 8 <= len(password) <= 128:
            raise UserError("密码长度为 8-128 位", "password")
        if must_change:
            if username == DEFAULT_USER:
                raise UserError("用户名不能使用默认值", "username")
            if password == DEFAULT_PASS:
                raise UserError("密码不能使用默认值", "password")
        with conn:
            set_setting(conn, "admin_user", username)
            if password:
                set_setting(conn, "admin_hash", hash_password(password))
            set_setting(conn, "must_change", "0")
            conn.execute("DELETE FROM sessions WHERE token_hash != ?", (session,))
        self.send_json(200, {"username": username})

    # 节点

    def node_brief(self, conn, n, now):
        return {"id": n["id"], "name": n["name"], "note": n["note"], "addr": n["addr"],
                "status": node_status(conn, n, now),
                "last_seen": n["last_seen"]}

    def api_nodes(self, conn, body):
        now = int(time.time())
        nodes = []
        for n in conn.execute("SELECT * FROM nodes ORDER BY id").fetchall():
            nodes.append({**self.node_brief(conn, n, now), "bar": node_bar(conn, n, now)})
        return {"now": now, "nodes": nodes}

    def api_node_add(self, conn, body):
        name = check_remark(body.get("name"), "name", "名称")
        note = check_note(body.get("note"))
        addr = check_host(body.get("addr"), "addr")
        with conn:
            cur = conn.execute("INSERT INTO nodes (name, note, addr, key) VALUES (?, ?, ?, ?)",
                               (name, note, addr, new_key()))
            add_event(conn, cur.lastrowid, f"创建节点，入站地址 {addr}")
        return {"id": cur.lastrowid}

    def api_node_get(self, conn, body, node_id):
        now = int(time.time())
        n = get_node(conn, node_id)
        rules = node_rule_rows(conn, n)
        chains = conn.execute(
            """SELECT DISTINCT c.id, c.remark FROM hops h JOIN chains c ON c.id = h.chain_id
               WHERE h.node_id = ? ORDER BY c.id""", (node_id,)).fetchall()
        events = conn.execute(
            "SELECT ts, text FROM events WHERE node_id = ? ORDER BY ts DESC, id DESC LIMIT ?",
            (node_id, EVENTS_SHOWN)).fetchall()
        return {
            "now": now,
            "sync_path": setting(conn, "sync_path"),
            "public_url": setting(conn, "public_url", ""),
            "agent_url": AGENT_URL,
            "version": VERSION,
            "node": {
                **self.node_brief(conn, n, now), "bar": node_bar(conn, n, now), "key": n["key"],
                "first_seen": n["first_seen"], "src_ip": n["src_ip"], "skew": n["skew"], "agent": n["agent"],
                "reported_v": n["reported_v"], "want_v": rules_version(node_rules(conn, n)),
                "state": n["state"], "err": n["err"],
            },
            "rules": rules,
            "chains": [dict(c) for c in chains],
            "events": [dict(e) for e in events],
        }

    def api_node_edit(self, conn, body, node_id):
        n = get_node(conn, node_id)
        name = check_remark(body["name"], "name", "名称") if "name" in body else n["name"]
        note = check_note(body["note"]) if "note" in body else n["note"]
        addr = check_host(body["addr"], "addr") if "addr" in body else n["addr"]
        key = None
        with conn:
            conn.execute("UPDATE nodes SET name = ?, note = ?, addr = ? WHERE id = ?", (name, note, addr, node_id))
            if addr != n["addr"]:
                key = new_key()
                conn.execute("UPDATE nodes SET key = ? WHERE id = ?", (key, node_id))
                add_event(conn, node_id, f"入站地址 {n['addr']} → {addr}，已重新生成密钥")
                for r in conn.execute(
                        """SELECT h.chain_id, h.node_id FROM hops h
                           JOIN hops h2 ON h2.chain_id = h.chain_id AND h2.seq = h.seq + 1
                           WHERE h2.node_id = ?""", (node_id,)).fetchall():
                    mark_pending(conn, r["chain_id"], [r["node_id"]])
        return {"key": key, "upstream": upstream_nodes(conn, node_id) if key else []}

    def api_node_rekey(self, conn, body, node_id):
        get_node(conn, node_id)
        key = new_key()
        with conn:
            conn.execute("UPDATE nodes SET key = ? WHERE id = ?", (key, node_id))
            add_event(conn, node_id, "重置密钥")
        return {"key": key}

    def api_node_del(self, conn, body, node_id):
        n = get_node(conn, node_id)
        used = [r[0] for r in conn.execute(
            """SELECT DISTINCT c.remark FROM hops h JOIN chains c ON c.id = h.chain_id
               WHERE h.node_id = ? ORDER BY c.id""", (node_id,))]
        if used:
            raise UserError(f"节点仍被转发使用：{'、'.join(used)}。请先删除或修改这些转发。")
        force = self.query.get("force") == ["1"]
        with conn:
            if force or not n["first_seen"]:
                delete_node(conn, node_id)
                return {"deleted": True}
            if not n["deleting"]:
                conn.execute("UPDATE nodes SET deleting = 1 WHERE id = ?", (node_id,))
                add_event(conn, node_id, "开始删除，等待节点清空规则")
        return {"deleted": False}

    # 转发

    def api_chains(self, conn, body):
        now = int(time.time())
        nodes = {n["id"]: n for n in conn.execute("SELECT * FROM nodes").fetchall()}
        status = {nid: node_status(conn, n, now) for nid, n in nodes.items()}
        node_info = lambda nid: {"node_id": nid, "name": nodes[nid]["name"], "status": status[nid]}
        chains = []
        for c in conn.execute("SELECT * FROM chains ORDER BY id").fetchall():
            hops = [{**node_info(h["node_id"]), "addr": nodes[h["node_id"]]["addr"], "port": h["port"]}
                    for h in conn.execute("SELECT * FROM hops WHERE chain_id = ? ORDER BY seq", (c["id"],))]
            pending = [node_info(r["node_id"]) for r in conn.execute(
                "SELECT node_id FROM chain_pending WHERE chain_id = ? ORDER BY node_id", (c["id"],))]
            chains.append({"id": c["id"], "remark": c["remark"], "target_host": c["target_host"],
                           "target_port": c["target_port"], "deleting": bool(c["deleting"]),
                           "hops": hops, "pending": pending})
        return {"chains": chains}

    def chain_spec(self, conn, chain_id):
        """转发的实际内容（跳和目标），用于判断改动是否影响规则。"""
        c = get_chain(conn, chain_id)
        hops = [(h["node_id"], h["port"]) for h in conn.execute(
            "SELECT node_id, port FROM hops WHERE chain_id = ? ORDER BY seq", (chain_id,))]
        return hops, (c["target_host"], c["target_port"])

    def api_chain_save(self, conn, body, chain_id=None):
        old = None
        if chain_id is not None:
            if get_chain(conn, chain_id)["deleting"]:
                raise UserError("该转发正在删除，不能修改")
            old = self.chain_spec(conn, chain_id)
        remark = check_remark(body.get("remark"))
        hops = parse_hops(conn, body.get("hops"), chain_id)
        host = check_host(body.get("target_host"), "target_host")
        port = check_port(body.get("target_port"), "target_port")
        with conn:
            if chain_id is None:
                chain_id = conn.execute("INSERT INTO chains (remark, target_host, target_port) VALUES (?, ?, ?)",
                                        (remark, host, port)).lastrowid
            else:
                conn.execute("UPDATE chains SET remark = ?, target_host = ?, target_port = ? WHERE id = ?",
                             (remark, host, port, chain_id))
            save_hops(conn, chain_id, hops)
            # 规则有变化时，改动前后涉及的节点都需要确认（被移出的节点要清掉旧规则）
            if old != (hops, (host, port)):
                mark_pending(conn, chain_id, [nid for nid, _ in hops] + [nid for nid, _ in (old or ([], None))[0]])
        return {"id": chain_id}

    def api_chain_del(self, conn, body, chain_id):
        get_chain(conn, chain_id)
        force = self.query.get("force") == ["1"]
        with conn:
            if force:
                delete_chain(conn, chain_id)
                return {"deleted": True}
            conn.execute("UPDATE chains SET deleting = 1 WHERE id = ?", (chain_id,))
            # 只需等待连接过的节点清理规则；之前未确认的改动一并以删除为准
            conn.execute("DELETE FROM chain_pending WHERE chain_id = ?", (chain_id,))
            mark_pending(conn, chain_id, [r[0] for r in conn.execute(
                """SELECT h.node_id FROM hops h JOIN nodes n ON n.id = h.node_id
                   WHERE h.chain_id = ? AND n.first_seen IS NOT NULL""", (chain_id,))])
            finish_deleted_chains(conn)
            deleted = not conn.execute("SELECT 1 FROM chains WHERE id = ?", (chain_id,)).fetchone()
        return {"deleted": deleted}

    # 设置

    def api_settings(self, conn, body):
        return {"public_url": setting(conn, "public_url", ""), "sync_path": setting(conn, "sync_path"),
                "panel_path": setting(conn, "panel_path"), "username": setting(conn, "admin_user"),
                "version": VERSION}

    def api_settings_save(self, conn, body):
        url = str(body.get("public_url", "")).strip().rstrip("/")
        if url and not re.fullmatch(r"https?://[^/\s]+", url):
            raise UserError("格式应为 http(s)://域名或IP[:端口]，不带路径", "public_url")
        with conn:
            set_setting(conn, "public_url", url)
        return {"public_url": url}


# ---------------------------------------------------------------- 命令行

def print_info(conn, listen=None):
    base = setting(conn, "public_url", "") or f"http://<服务器地址>:{(listen or '0.0.0.0:8730').rpartition(':')[2]}"
    print(f"  面板：    {base}{setting(conn, 'panel_path')}/")
    print(f"  同步路径：{setting(conn, 'sync_path')}")
    if setting(conn, "must_change") == "1":
        print(f"  默认账号：{DEFAULT_USER} / {DEFAULT_PASS}（首次登录后须修改）")


def cleanup_loop():
    while True:
        try:
            conn = db()
            cleanup(conn)
            conn.close()
        except Exception as e:
            print(f"cleanup error: {e!r}", file=sys.stderr)
        time.sleep(3600)


def cmd_serve(args):
    if not shutil.which("openssl"):
        raise UserError("缺少依赖：openssl")
    missing = [f for f in STATIC if not os.path.isfile(os.path.join(WEB_DIR, f))]
    if missing:
        raise UserError(f"缺少面板文件：{', '.join(missing)}（应位于 {WEB_DIR}）")
    conn = init_db()
    Handler.sync_path = setting(conn, "sync_path")
    Handler.panel_path = setting(conn, "panel_path")
    host, _, port = args.listen.rpartition(":")
    server = http.server.ThreadingHTTPServer((host or "0.0.0.0", int(port)), Handler)
    print(f"ictl-server {VERSION} 监听 {args.listen}", flush=True)
    print_info(conn, args.listen)
    sys.stdout.flush()
    conn.close()
    threading.Thread(target=cleanup_loop, daemon=True).start()
    server.serve_forever()


def cmd_info(args):
    print_info(init_db())


def cmd_reset_admin(args):
    conn = init_db()
    with conn:
        reset_admin(conn)
    print(f"管理员已重置为 {DEFAULT_USER} / {DEFAULT_PASS}，所有登录会话已失效。首次登录后须修改。")


def main():
    p = argparse.ArgumentParser(prog="ictl-server", description=f"ictl 服务端 {VERSION}")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("serve", help="运行服务（首次运行自动初始化）")
    s.add_argument("--listen", default="0.0.0.0:8730", help="监听地址，默认 0.0.0.0:8730")
    s.set_defaults(func=cmd_serve)
    s = sub.add_parser("info", help="显示面板地址和同步路径")
    s.set_defaults(func=cmd_info)
    s = sub.add_parser("reset-admin", help="重置管理员账号为默认值")
    s.set_defaults(func=cmd_reset_admin)

    args = p.parse_args()
    os.umask(0o077)  # 数据库保存密钥原文，只允许属主读写
    try:
        args.func(args)
    except UserError as e:
        print(f"错误：{e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
