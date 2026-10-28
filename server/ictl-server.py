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
import tunnel_config

VERSION = "0.2.1"
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
with open(os.path.join(BASE_DIR, "components.json")) as _f:
    COMPONENTS = json.load(_f)
FORWARD_VERSION = COMPONENTS["forward_agent"]
TUNNEL_VERSION = COMPONENTS["tunnel_agent"]
REPO = "moran1024a/iptables-control"
AGENT_URL = f"https://github.com/{REPO}/releases/download/v{VERSION}/ictl.sh"
DB_PATH = os.environ.get("ICTL_DB", "/var/lib/ictl-server/ictl.db")
WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")
# 同版本的节点端脚本，供节点自动更新时下载：Docker 镜像中为 /app/agent/ictl.sh，源码目录中为 ../agent/ictl.sh
AGENT_FILES = [os.path.join(os.path.dirname(os.path.abspath(__file__)), p)
               for p in ("agent/ictl.sh", "../agent/ictl.sh")]
AGENT_AUTO_UPDATE = (0, 1, 7)    # 从该版本起，节点端按服务端的更新信号自动更新

OFFLINE_AFTER = 120         # 超过该秒数没有上报视为离线（节点每 20-40 秒上报一次）
MAX_SKEW = 60               # 节点时间允许的最大偏差（秒）
MAX_BODY = 8192             # 同步请求体上限
MAX_API_BODY = 65536        # 面板请求体上限
WINDOW = 60                 # 状态条时间窗口（秒），需大于节点的最长上报间隔
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
KINDS = ("nodes", "tunnel_nodes", "routes", "tunnels", "chains")  # 五套独立的分组与排序
DEFAULT_GROUP = "默认分组"

SCHEMA = """
CREATE TABLE IF NOT EXISTS item_groups (
  id INTEGER PRIMARY KEY,
  kind TEXT NOT NULL,
  name TEXT NOT NULL,
  builtin INTEGER NOT NULL DEFAULT 0,
  sort INTEGER NOT NULL DEFAULT 0
);
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
  health TEXT,
  sort INTEGER NOT NULL DEFAULT 0,
  ports TEXT NOT NULL DEFAULT '10000-65535',
  group_id INTEGER NOT NULL DEFAULT 0,
  auto_update INTEGER
);
CREATE TABLE IF NOT EXISTS chains (
  id INTEGER PRIMARY KEY,
  remark TEXT NOT NULL,
  target_host TEXT NOT NULL,
  target_port INTEGER NOT NULL,
  deleting INTEGER NOT NULL DEFAULT 0,
  sort INTEGER NOT NULL DEFAULT 0,
  group_id INTEGER NOT NULL DEFAULT 0
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
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version > 202:
        conn.close()
        raise UserError("数据库版本高于本服务端，请使用兼容版本")
    if version == 202:
        return conn
    # 备份旧库，包括尚未 checkpoint 的 WAL；迁移失败时旧数据仍然可恢复。
    exists = conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'nodes'").fetchone()
    if exists:
        backup_path = DB_PATH + (".pre-0.2.0.bak" if version < 200 else
                                 ".pre-layout-201.bak" if version == 200 else ".pre-0.2.1.bak")
        if not os.path.exists(backup_path):
            temporary = backup_path + ".tmp"
            backup = sqlite3.connect(temporary)
            try:
                conn.backup(backup)
            finally:
                backup.close()
            os.chmod(temporary, 0o600)
            os.replace(temporary, backup_path)
    conn.execute("PRAGMA journal_mode = WAL")
    if version == 201:
        migrate_imports(conn)
        return conn
    if version == 200:
        migrate_layout(conn)
        migrate_imports(conn)
        return conn
    conn.executescript(SCHEMA)
    # 旧版本升级
    cols = lambda t: [r["name"] for r in conn.execute(f"PRAGMA table_info({t})")]
    if "deleting" not in cols("chains"):                   # 0.1.0：转发删除确认
        conn.execute("ALTER TABLE chains ADD COLUMN deleting INTEGER NOT NULL DEFAULT 0")
    if "remark" in cols("nodes"):                          # 0.1.1：节点「备注」改为「名称」，新增「备注」
        conn.execute("ALTER TABLE nodes RENAME COLUMN remark TO name")
    if "note" not in cols("nodes"):
        conn.execute("ALTER TABLE nodes ADD COLUMN note TEXT NOT NULL DEFAULT ''")
    for t in ("nodes", "chains"):                          # 0.1.4：自定义排序，按原有顺序初始化
        if "sort" not in cols(t):
            conn.execute(f"ALTER TABLE {t} ADD COLUMN sort INTEGER NOT NULL DEFAULT 0")
            conn.execute(f"UPDATE {t} SET sort = id")
    if "ports" not in cols("nodes"):                      # 0.1.4：节点允许的端口，默认 10000-65535，并包含已在用的端口
        conn.execute("ALTER TABLE nodes ADD COLUMN ports TEXT NOT NULL DEFAULT '10000-65535'")
        conn.execute("""UPDATE nodes SET ports = (SELECT MIN(port) FROM hops WHERE hops.node_id = nodes.id) || '-65535'
                        WHERE (SELECT MIN(port) FROM hops WHERE hops.node_id = nodes.id) < 10000""")
    for t in ("nodes", "chains"):                          # 0.1.6：分组，原有节点和转发放入默认分组
        if "group_id" not in cols(t):
            conn.execute(f"ALTER TABLE {t} ADD COLUMN group_id INTEGER NOT NULL DEFAULT 0")
        if not conn.execute("SELECT 1 FROM item_groups WHERE kind = ? AND builtin = 1", (t,)).fetchone():
            conn.execute("INSERT INTO item_groups (kind, name, builtin, sort) VALUES (?, ?, 1, 0)", (t, DEFAULT_GROUP))
        conn.execute(f"UPDATE {t} SET group_id = ? WHERE group_id NOT IN (SELECT id FROM item_groups WHERE kind = ?)",
                     (default_group(conn, t), t))
    if "auto_update" not in cols("nodes"):                 # 0.1.7：节点是否开启自动更新（旧版节点为空）
        conn.execute("ALTER TABLE nodes ADD COLUMN auto_update INTEGER")
    old_window = int(setting(conn, "window", 30))           # 0.1.7：状态窗口 30 秒 → 60 秒，合并旧数据并取较差的状态
    if old_window != WINDOW:
        rows = conn.execute("SELECT node_id, win, level FROM windows").fetchall()
        conn.execute("DELETE FROM windows")
        for r in rows:
            conn.execute("""INSERT INTO windows VALUES (?, ?, ?)
                            ON CONFLICT (node_id, win) DO UPDATE SET level = MIN(level, excluded.level)""",
                         (r["node_id"], r["win"] * old_window // WINDOW, r["level"]))
        set_setting(conn, "window", str(WINDOW))
    conn.commit()
    with conn:
        if not setting(conn, "sync_path"):
            set_setting(conn, "sync_path", "/" + secrets.token_hex(12))
        if not setting(conn, "panel_path"):
            set_setting(conn, "panel_path", "/" + secrets.token_hex(8))
        if not setting(conn, "admin_user"):
            reset_admin(conn)
    migrate_v020(conn)
    migrate_layout(conn)
    migrate_imports(conn)
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


def default_group(conn, kind):
    return conn.execute("SELECT id FROM item_groups WHERE kind = ? AND builtin = 1", (kind,)).fetchone()[0]


def get_groups(conn, kind):
    return [dict(r) for r in conn.execute(
        "SELECT id, name, builtin FROM item_groups WHERE kind = ? ORDER BY sort, id", (kind,))]


def check_group(conn, kind, group_id):
    """校验所选分组；未选择时为默认分组。"""
    if group_id in (None, ""):
        return default_group(conn, kind)
    if isinstance(group_id, int) and conn.execute(
            "SELECT 1 FROM item_groups WHERE id = ? AND kind = ?", (group_id, kind)).fetchone():
        return group_id
    raise UserError("分组不存在，请刷新后重试", "group_id")


def ordered(kind, where=""):
    """按分组顺序、再按组内顺序查询节点或转发。"""
    return f"""SELECT t.* FROM {kind} t JOIN item_groups g ON g.id = t.group_id
               {where} ORDER BY g.sort, g.id, t.sort, t.id"""


def get_node(conn, node_id):
    if type(node_id) is not int or node_id <= 0:
        raise UserError("无效的对象 ID", "node_id")
    node = conn.execute("SELECT * FROM all_nodes WHERE id = ?", (node_id,)).fetchone()
    if not node:
        raise UserError("节点不存在", status=404)
    return node


def get_chain(conn, chain_id):
    if type(chain_id) is not int or chain_id <= 0:
        raise UserError("无效的对象 ID", "chain_id")
    chain = conn.execute("SELECT * FROM chains WHERE id = ?", (chain_id,)).fetchone()
    if not chain:
        raise UserError("转发不存在", status=404)
    return chain


def delete_node(conn, node_id):
    table = node_table(get_node(conn, node_id)["kind"])
    conn.execute("DELETE FROM retired_ports WHERE node_id = ?", (node_id,))
    conn.execute("DELETE FROM tunnel_presence WHERE node_id = ?", (node_id,))
    conn.execute(f"DELETE FROM {table} WHERE id = ?", (node_id,))
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
    active = {r[0] for r in conn.execute("SELECT node_id FROM hops WHERE chain_id = ?", (chain_id,))}
    for nid in set(node_ids):
        node = conn.execute("SELECT first_seen FROM nodes WHERE id = ?", (nid,)).fetchone()
        if node and (nid in active or node["first_seen"]):
            conn.execute("INSERT OR IGNORE INTO chain_pending VALUES (?, ?)", (chain_id, nid))
    conn.execute("""DELETE FROM chain_pending WHERE chain_id = ?
                    AND node_id NOT IN (SELECT node_id FROM hops WHERE chain_id = ?)
                    AND node_id IN (SELECT id FROM nodes WHERE first_seen IS NULL)""", (chain_id, chain_id))


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
        conn.execute("DELETE FROM tunnel_presence WHERE end < ?", (now - RETENTION,))


def migrate_v020(conn):
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version > 200:
        raise UserError("数据库版本高于本服务端，请使用兼容版本")
    if version == 200:
        return
    # 物理表 chains/hops 继续保存实际配置，避免重编号或改变规则顺序。
    before = {n["id"]: node_rules(conn, n) for n in conn.execute("SELECT * FROM nodes")}
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("ALTER TABLE nodes ADD COLUMN kind TEXT NOT NULL DEFAULT 'forward'")
        conn.execute("ALTER TABLE nodes ADD COLUMN core_version TEXT")
        conn.execute("ALTER TABLE nodes ADD COLUMN online_until INTEGER NOT NULL DEFAULT 0")
        conn.execute("CREATE TABLE routes (id INTEGER PRIMARY KEY, name TEXT NOT NULL, node_ids TEXT NOT NULL)")
        conn.execute("""CREATE TABLE tunnels (
            id INTEGER PRIMARY KEY, name TEXT NOT NULL, node_id INTEGER NOT NULL REFERENCES nodes(id),
            protocol TEXT NOT NULL, port INTEGER NOT NULL, params TEXT NOT NULL,
            deleting INTEGER NOT NULL DEFAULT 0, UNIQUE(node_id, port))""")
        conn.execute("ALTER TABLE chains ADD COLUMN route_id INTEGER REFERENCES routes(id)")
        conn.execute("ALTER TABLE chains ADD COLUMN tunnel_id INTEGER REFERENCES tunnels(id)")
        conn.execute("ALTER TABLE chains ADD COLUMN ready INTEGER NOT NULL DEFAULT 1")
        conn.execute("""CREATE TABLE retired_ports (
            node_id INTEGER NOT NULL REFERENCES nodes(id), port INTEGER NOT NULL,
            PRIMARY KEY(node_id, port))""")
        conn.execute("""CREATE TABLE tunnel_presence (
            node_id INTEGER NOT NULL REFERENCES nodes(id), start INTEGER NOT NULL, end INTEGER NOT NULL,
            PRIMARY KEY(node_id, start))""")
        for c in conn.execute("SELECT * FROM chains ORDER BY id").fetchall():
            ids = [h[0] for h in conn.execute("SELECT node_id FROM hops WHERE chain_id = ? ORDER BY seq", (c["id"],))]
            rid = conn.execute("INSERT INTO routes(name, node_ids) VALUES (?, ?)",
                               (c["remark"], json.dumps(ids))).lastrowid
            conn.execute("UPDATE chains SET route_id = ? WHERE id = ?", (rid, c["id"]))
        set_setting(conn, "next_hop_id", str(conn.execute("SELECT COALESCE(MAX(id), 0) + 1 FROM hops").fetchone()[0]))
        set_setting(conn, "tunnel_path", "/" + secrets.token_hex(12))
        set_setting(conn, "tunnel_heartbeat", "10")
        after = {n["id"]: node_rules(conn, n) for n in conn.execute("SELECT * FROM nodes")}
        if before != after:
            raise UserError("升级校验失败：转发规则发生变化，迁移已回滚")
        conn.execute("PRAGMA user_version = 200")


def migrate_layout(conn):
    """拆分节点存储与五类分组；不重编号节点、凭据、转发规则。"""
    original = [dict(n) for n in conn.execute("SELECT * FROM nodes ORDER BY id")]
    before = {n["id"]: node_rules(conn, n) for n in original if n["kind"] == "forward"}
    conn.execute("PRAGMA foreign_keys = OFF")
    try:
        with conn:
            conn.execute("BEGIN IMMEDIATE")
            schema = conn.execute("SELECT sql FROM sqlite_master WHERE name = 'nodes'").fetchone()[0]
            schema = re.sub(r'CREATE TABLE (?:"nodes"|nodes)', "CREATE TABLE tunnel_nodes", schema, count=1)
            schema = schema.replace("DEFAULT 'forward'", "DEFAULT 'tunnel'")
            conn.execute(schema)
            conn.execute("INSERT INTO tunnel_nodes SELECT * FROM nodes WHERE kind = 'tunnel'")
            # 仅迁移隧道节点实际使用的分组，避免复制仅属于转发节点的空分组。
            for g in conn.execute("""SELECT * FROM item_groups WHERE kind = 'nodes'
                    AND (builtin = 1 OR id IN (SELECT group_id FROM tunnel_nodes)) ORDER BY id""").fetchall():
                gid = conn.execute("INSERT INTO item_groups(kind,name,builtin,sort) VALUES ('tunnel_nodes',?,?,?)",
                                   (g["name"], g["builtin"], g["sort"])).lastrowid
                conn.execute("UPDATE tunnel_nodes SET group_id = ? WHERE group_id = ?", (gid, g["id"]))
            for table, reference in (("tunnels", "REFERENCES tunnel_nodes(id)"),
                                     ("tunnel_presence", "REFERENCES tunnel_nodes(id)"),
                                     ("retired_ports", "")):
                ddl = conn.execute("SELECT sql FROM sqlite_master WHERE name = ?", (table,)).fetchone()[0]
                ddl = re.sub(r'CREATE TABLE (?:"' + table + r'"|' + table + r')', "CREATE TABLE " + table + "_new", ddl, count=1)
                ddl = re.sub(r'REFERENCES\s+nodes\s*\(id\)', reference, ddl)
                conn.execute(ddl)
                conn.execute(f"INSERT INTO {table}_new SELECT * FROM {table}")
                conn.execute(f"DROP TABLE {table}")
                conn.execute(f"ALTER TABLE {table}_new RENAME TO {table}")
            conn.execute("DELETE FROM nodes WHERE kind = 'tunnel'")
            conn.execute("CREATE VIEW all_nodes AS SELECT * FROM nodes UNION ALL SELECT * FROM tunnel_nodes")
            set_setting(conn, "next_node_id", str(conn.execute("SELECT COALESCE(MAX(id), 0) + 1 FROM all_nodes").fetchone()[0]))
            for table in ("routes", "tunnels"):
                gid = conn.execute("INSERT INTO item_groups(kind,name,builtin,sort) VALUES (?,?,1,0)",
                                   (table, DEFAULT_GROUP)).lastrowid
                conn.execute(f"ALTER TABLE {table} ADD COLUMN group_id INTEGER NOT NULL DEFAULT 0")
                conn.execute(f"ALTER TABLE {table} ADD COLUMN sort INTEGER NOT NULL DEFAULT 0")
                conn.execute(f"UPDATE {table} SET group_id = ?, sort = id", (gid,))
            if conn.execute("PRAGMA foreign_key_check").fetchall():
                raise UserError("节点隔离迁移失败：引用完整性校验未通过")
            migrated = [dict(n) for n in conn.execute("SELECT * FROM all_nodes ORDER BY id")]
            for rows in (original, migrated):
                for n in rows:
                    n.pop("group_id")  # 仅分组 ID 在拆分时变化，其余节点数据必须完全一致。
            if original != migrated:
                raise UserError("节点隔离迁移失败：节点记录不一致")
            after = {n["id"]: node_rules(conn, n) for n in conn.execute("SELECT * FROM nodes")}
            if before != after:
                raise UserError("节点隔离迁移失败：转发规则发生变化")
            conn.execute("PRAGMA user_version = 201")
    finally:
        conn.execute("PRAGMA foreign_keys = ON")


def migrate_imports(conn):
    """允许外部隧道不关联 Agent，保留已有隧道 ID、端口、凭据与引用。"""
    conn.execute("PRAGMA foreign_keys = OFF")
    try:
        with conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("""CREATE TABLE tunnels_new (
                id INTEGER PRIMARY KEY, name TEXT NOT NULL,
                node_id INTEGER REFERENCES tunnel_nodes(id),
                protocol TEXT NOT NULL, port INTEGER NOT NULL, params TEXT NOT NULL,
                deleting INTEGER NOT NULL DEFAULT 0, group_id INTEGER NOT NULL DEFAULT 0,
                sort INTEGER NOT NULL DEFAULT 0, source TEXT NOT NULL DEFAULT 'agent',
                external_host TEXT,
                CHECK ((source = 'agent' AND node_id IS NOT NULL AND external_host IS NULL)
                    OR (source = 'external' AND node_id IS NULL AND external_host IS NOT NULL)),
                UNIQUE(node_id, port))""")
            conn.execute("""INSERT INTO tunnels_new(id,name,node_id,protocol,port,params,deleting,group_id,sort)
                SELECT id,name,node_id,protocol,port,params,deleting,group_id,sort FROM tunnels""")
            conn.execute("DROP TABLE tunnels")
            conn.execute("ALTER TABLE tunnels_new RENAME TO tunnels")
            if conn.execute("PRAGMA foreign_key_check").fetchall():
                raise UserError("隧道导入升级失败：引用完整性校验未通过")
            conn.execute("PRAGMA user_version = 202")
    finally:
        conn.execute("PRAGMA foreign_keys = ON")


def node_table(kind):
    if kind not in ("forward", "tunnel"):
        raise UserError("节点类型无效", "kind")
    return "tunnel_nodes" if kind == "tunnel" else "nodes"


def move_group(conn, table, item_id, group_id):
    """换组后排在新组末尾，不改变业务配置或下发版本。"""
    conn.execute(f"""UPDATE {table} SET group_id = ?, sort =
                 (SELECT COALESCE(MAX(sort), 0) + 1 FROM {table} WHERE group_id = ?)
                 WHERE id = ?""", (group_id, group_id, item_id))


def heartbeat_interval(conn):
    return int(setting(conn, "tunnel_heartbeat", "10"))


def get_route(conn, route_id):
    if type(route_id) is not int or route_id <= 0:
        raise UserError("无效的对象 ID", "route_id")
    r = conn.execute("SELECT * FROM routes WHERE id = ?", (route_id,)).fetchone()
    if not r:
        raise UserError("链路不存在", "route_id", 404)
    return r


def get_tunnel(conn, tunnel_id):
    if type(tunnel_id) is not int or tunnel_id <= 0:
        raise UserError("无效的对象 ID", "tunnel_id")
    t = conn.execute("SELECT * FROM tunnels WHERE id = ?", (tunnel_id,)).fetchone()
    if not t:
        raise UserError("隧道不存在", "tunnel_id", 404)
    return t


def tunnel_snapshot(conn, node):
    ts = [] if node["deleting"] else conn.execute(
        "SELECT * FROM tunnels WHERE node_id = ? AND deleting = 0 ORDER BY id", (node["id"],)).fetchall()
    config = tunnel_config.core_config(ts)
    return {"api": 1, "revision": tunnel_config.revision({"config": config, "core_version": COMPONENTS["sing_box"]["version"]}), "config": config,
            "core_version": COMPONENTS["sing_box"]["version"]}


def tunnel_ready(conn, node_id):
    n = get_node(conn, node_id)
    return n["state"] == "ok" and n["reported_v"] == tunnel_snapshot(conn, n)["revision"]


def tunnel_bar(conn, node, now):
    start = (now // 10 - 59) * 10
    spans = conn.execute("SELECT start, end FROM tunnel_presence WHERE node_id = ? AND end >= ?",
                         (node["id"], start)).fetchall()
    levels = []
    for ts in range(start, start + 600, 10):
        point = min(ts + 9, now)
        if not node["first_seen"] or point < node["first_seen"]:
            levels.append("none")
        else:
            levels.append("ok" if any(s[0] <= point <= s[1] for s in spans) else "lost")
    return {"start": start, "window": 10, "levels": levels}


def reserve_removed(conn, node_id, port):
    # 不关联配置外键，强删配置也不能提前复用尚未确认移除的旧端口。
    n = get_node(conn, node_id)
    if n["first_seen"]:
        conn.execute("INSERT OR IGNORE INTO retired_ports VALUES (?, ?)", (node_id, port))


def apply_config_hops(conn, chain_id, pairs):
    old = conn.execute("SELECT * FROM hops WHERE chain_id = ? ORDER BY seq", (chain_id,)).fetchall()
    wanted = set(pairs)
    for h in old:
        if (h["node_id"], h["port"]) not in wanted:
            reserve_removed(conn, h["node_id"], h["port"])
            conn.execute("DELETE FROM hops WHERE id = ?", (h["id"],))
    existing = {(h["node_id"], h["port"]): h["id"] for h in old}
    for seq, pair in enumerate(pairs):
        if pair in existing:
            conn.execute("UPDATE hops SET seq = ? WHERE id = ?", (seq, existing[pair]))
        else:
            hid = int(setting(conn, "next_hop_id"))
            set_setting(conn, "next_hop_id", str(hid + 1))
            conn.execute("INSERT INTO hops(id, chain_id, seq, node_id, port) VALUES (?, ?, ?, ?, ?)",
                         (hid, chain_id, seq, *pair))


def route_hops(conn, route, chain_id=None, ports=None):
    ids = json.loads(route["node_ids"])
    if ports is not None and (not isinstance(ports, list) or len(ports) != len(ids)):
        raise UserError("端口数量必须与链路跳数一致", "ports")
    old = list(conn.execute("SELECT * FROM hops WHERE chain_id = ? ORDER BY seq", (chain_id,)))
    hops = []
    for i, nid in enumerate(ids):
        match = next((h for h in old if h["node_id"] == nid), None)
        if match:
            old.remove(match)
        port = ports[i] if ports is not None else match["port"] if match else None
        hops.append({"node_id": nid, "port": port})
    return parse_hops(conn, hops, chain_id)


def mark_config_nodes(conn, chain_id):
    mark_pending(conn, chain_id, [r[0] for r in conn.execute("SELECT node_id FROM hops WHERE chain_id = ?", (chain_id,))])


def block_tunnel_configs(conn, tunnel_id):
    for c in conn.execute("SELECT id FROM chains WHERE tunnel_id = ? AND deleting = 0", (tunnel_id,)).fetchall():
        conn.execute("UPDATE chains SET ready = 0 WHERE id = ?", (c["id"],))
        mark_config_nodes(conn, c["id"])


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


# ---------------------------------------------------------------- 节点端更新

def ver(s):
    """版本号转为可比较的数字元组，如 "0.1.10" → (0, 1, 10)。"""
    return tuple(int(x) for x in re.findall(r"\d+", str(s or "")))


AGENT = None                # (版本, sha256, 内容)；找不到同版本的节点端脚本时为 None


def load_agent():
    global AGENT
    for path in AGENT_FILES:
        if os.path.isfile(path):
            with open(path, "rb") as f:
                data = f.read()
            m = re.search(rb'^VERSION="([^"]+)"', data, re.M)
            if m and m[1].decode() == FORWARD_VERSION:
                AGENT = (FORWARD_VERSION, hashlib.sha256(data).hexdigest(), data)
                return
            print(f"节点端脚本 {path} 与组件清单不一致，节点不会自动更新", file=sys.stderr)
            return
    print("未找到节点端脚本，节点不会自动更新", file=sys.stderr)


def agent_update(agent, auto_update):
    """节点端的更新情况：same / newer / pending / disabled / legacy / unavailable。"""
    if not agent or ver(agent) == ver(FORWARD_VERSION):
        return "same"
    if ver(agent) > ver(FORWARD_VERSION):
        return "newer"
    if ver(agent) < AGENT_AUTO_UPDATE:
        return "legacy"         # 旧版节点端自行定时检查更新
    if not AGENT:
        return "unavailable"
    return "pending" if auto_update else "disabled"


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


def seal_bytes(key, req_tag, data):
    ek, mk = derive(key)
    msg = aes(ek, data)[8:]
    return base64.b64encode(msg + mac(mk, req_tag + msg)).decode()


def seal(key, req_tag, text):
    return seal_bytes(key, req_tag, text.encode())


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


def parse_ports(s, field="ports"):
    """解析允许的端口：逗号分隔的端口或范围，如 10000-20000,30000。返回排序合并后的 [(起始, 结束)]。"""
    s = str(s or "").replace("，", ",").replace(" ", "")
    if len(s) > 300:
        raise UserError("允许的端口最长 300 个字符", field)
    ranges = []
    for item in filter(None, s.split(",")):
        m = re.fullmatch(r"(\d{1,5})(?:-(\d{1,5}))?", item)
        if not m:
            raise UserError(f"格式错误：{item}（应为 端口 或 起始-结束，多个用逗号分隔）", field)
        lo, hi = int(m[1]), int(m[2] or m[1])
        if not 1 <= lo <= hi <= 65535:
            raise UserError(f"范围无效：{item}（端口为 1-65535，起始不能大于结束）", field)
        ranges.append((lo, hi))
    if not ranges:
        raise UserError("请填写允许的端口", field)
    merged = []
    for lo, hi in sorted(ranges):
        if merged and lo <= merged[-1][1] + 1:
            merged[-1] = (merged[-1][0], max(merged[-1][1], hi))
        else:
            merged.append((lo, hi))
    return merged


def format_ports(ranges):
    return ",".join(str(lo) if lo == hi else f"{lo}-{hi}" for lo, hi in ranges)


def in_ports(port, ranges):
    return any(lo <= port <= hi for lo, hi in ranges)


def pick_port(ranges, used):
    """在允许的端口中随机选一个未使用的端口；没有可用端口时返回 None。"""
    total = sum(hi - lo + 1 for lo, hi in ranges)
    if total <= sum(1 for p in used if in_ports(p, ranges)):
        return None
    for _ in range(64):
        k = secrets.randbelow(total)
        for lo, hi in ranges:
            if k <= hi - lo:
                port = lo + k
                break
            k -= hi - lo + 1
        if port not in used:
            return port
    return next(p for lo, hi in ranges for p in range(lo, hi + 1) if p not in used)


def check_note(s):
    s = str(s or "").strip().replace("\r\n", "\n")
    if len(s) > 500:
        raise UserError("备注最长 500 个字符", "note")
    return s


def parse_hops(conn, hops, chain_id=None):
    """把 [{node_id, port}] 解析为 [(node_id, port)]，检查端口冲突；端口留空时随机分配。
    返回 (跳列表, 自动分配说明列表)。"""
    if not isinstance(hops, list) or not hops:
        raise UserError("至少需要一跳", "hops")
    result = []
    for i, h in enumerate(hops, 1):
        nid = h.get("node_id") if isinstance(h, dict) else None
        node = conn.execute("SELECT * FROM nodes WHERE id = ?", (nid,)).fetchone() if isinstance(nid, int) else None
        if not node:
            raise UserError(f"第 {i} 跳：请选择节点", "hops")
        if node["kind"] != "forward":
            raise UserError(f"第 {i} 跳：只能使用转发节点", "hops")
        if node["deleting"]:
            raise UserError(f"第 {i} 跳：节点 {node['name']} 正在删除，不能使用", "hops")
        port = None
        if str(h.get("port") or "").strip():
            try:
                port = check_port(h.get("port"), "hops")
            except UserError:
                raise UserError(f"第 {i} 跳：端口必须是 1-65535 的整数，或留空自动分配", "hops")
            if not in_ports(port, parse_ports(node["ports"])):
                raise UserError(f"第 {i} 跳：端口 {port} 不在节点 {node['name']} 允许的端口 {node['ports']} 内", "hops")
        result.append([node, port])

    seen = set()
    for i, (node, port) in enumerate(result, 1):
        if port is None:
            continue
        if (node["id"], port) in seen:
            raise UserError(f"第 {i} 跳：重复使用了节点 {node['name']} 的端口 {port}", "hops")
        seen.add((node["id"], port))
        row = conn.execute(
            """SELECT c.remark || CASE c.deleting WHEN 1 THEN '（删除中）' ELSE '' END AS remark
               FROM hops h JOIN chains c ON c.id = h.chain_id
               WHERE h.node_id = ? AND h.port = ? AND h.chain_id IS NOT ?""",
            (node["id"], port, chain_id),
        ).fetchone()
        if conn.execute("SELECT 1 FROM retired_ports WHERE node_id = ? AND port = ?", (node["id"], port)).fetchone():
            raise UserError(f"第 {i} 跳：端口 {port} 尚在等待旧规则删除确认", "ports")
        if row:
            raise UserError(f"第 {i} 跳：节点 {node['name']} 的端口 {port} 已被转发「{row['remark']}」使用", "hops")

    # 留空的端口：避开该节点其他转发（含删除中）和本次已填写、已分配的端口
    assigned = []
    for i, item in enumerate(result, 1):
        node, port = item
        if port is not None:
            continue
        used = {r[0] for r in conn.execute(
            "SELECT port FROM hops WHERE node_id = ? AND chain_id IS NOT ?", (node["id"], chain_id))}
        used |= {r[0] for r in conn.execute("SELECT port FROM retired_ports WHERE node_id = ?", (node["id"],))}
        used |= {p for n, p in result if n["id"] == node["id"] and p is not None}
        item[1] = pick_port(parse_ports(node["ports"]), used)
        if item[1] is None:
            raise UserError(f"第 {i} 跳：节点 {node['name']} 允许的端口 {node['ports']} 已全部被占用", "hops")
        assigned.append(f"{node['name']}:{item[1]}")
    return [(node["id"], port) for node, port in result], assigned



# ---------------------------------------------------------------- 规则展开与状态

def node_rule_rows(conn, node):
    """节点应有的规则：非最后一跳指向下一跳节点，最后一跳指向链的目标。"""
    if node["deleting"]:
        return []
    ready = "AND c.ready = 1" if "ready" in [r["name"] for r in conn.execute("PRAGMA table_info(chains)")] else ""
    rows = conn.execute(
        f"""
        SELECT h.id, h.port, c.id AS chain_id, c.remark AS chain_remark, c.target_host, c.target_port,
               n2.addr AS next_addr, h2.port AS next_port
        FROM hops h
        JOIN chains c ON c.id = h.chain_id
        LEFT JOIN hops h2 ON h2.chain_id = h.chain_id AND h2.seq = h.seq + 1
        LEFT JOIN nodes n2 ON n2.id = h2.node_id
        WHERE h.node_id = ? AND c.deleting = 0 {ready}
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
    if node["kind"] == "tunnel":
        return "new" if not node["first_seen"] else "online" if now <= node["online_until"] else "offline"
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
    if node["kind"] == "tunnel":
        return tunnel_bar(conn, node, now)
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
    tunnel_path = "/tunnel-uninitialized"

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
            if path.startswith(self.tunnel_path + "/"):
                return self.tunnel_api(method, path[len(self.tunnel_path) + 1:])
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
        try:
            length = self.body_length(MAX_BODY)
        except UserError:
            return self.not_found()
        if length == 0:
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
            conn.execute("BEGIN IMMEDIATE")
            for node in conn.execute("SELECT * FROM nodes WHERE kind = 'forward'").fetchall():
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

            agent = fields.get("agent", "")[:32]
            with conn:
                record_report(conn, node, now, health, err, skew)
                if agent and node["agent"] and agent != node["agent"]:
                    add_event(conn, node["id"], f"节点端版本 {node['agent']} → {agent}", now)

                # 时间相差过大：只记录时差，不处理请求（防重放）
                if health == "skew":
                    conn.execute(
                        """UPDATE nodes SET first_seen = COALESCE(first_seen, ?), last_seen = ?, src_ip = ?,
                           skew = ?, health = ? WHERE id = ?""",
                        (now, now, self.source_ip(), skew, health, node["id"]),
                    )
                    return self.reply(200, seal(node["key"], req_tag, f"time={now}\n"))

                auto_update = {"1": 1, "0": 0}.get(fields.get("au"))
                conn.execute(
                    """UPDATE nodes SET first_seen = COALESCE(first_seen, ?), last_seen = ?, src_ip = ?,
                       reported_v = ?, state = ?, err = ?, agent = ?, skew = ?, health = ?, auto_update = ?
                       WHERE id = ?""",
                    (now, now, self.source_ip(), v, state, err, agent, skew, health, auto_update, node["id"]),
                )
                lines = node_rules(conn, node)
                want = rules_version(lines)

                if v == want and state == "ok":
                    conn.execute("DELETE FROM retired_ports WHERE node_id = ?", (node["id"],))
                    conn.execute("DELETE FROM chain_pending WHERE node_id = ?", (node["id"],))
                    finish_deleted_chains(conn)

                if node["deleting"] and v == want and state == "ok":
                    delete_node(conn, node["id"])
                    out = "same\n"
                elif v == want and state != "init":
                    out = "same\n"
                else:
                    out = "".join(f"{x}\n" for x in [f"v={want}"] + lines)
                # 节点端版本低于服务端：附带更新信号（目标版本和脚本哈希），节点再以 get=agent 下载脚本
                if agent_update(agent, auto_update) == "pending":
                    if fields.get("get") == "agent":
                        return self.reply(200, seal_bytes(node["key"], req_tag, AGENT[2]))
                    out += f"update={AGENT[0]} {AGENT[1]}\n"
            self.reply(200, seal(node["key"], req_tag, out))
        finally:
            conn.close()

    # ------------------------------------------------------------ 面板接口

    def api(self, method, route, query):
        conn = db()
        try:
            # 修改类请求必须是 JSON，配合 SameSite=Strict 防跨站请求
            if method != "GET" and not self.headers.get("Content-Type", "").startswith("application/json"):
                self.close_connection = True
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
                return self.send_json(200, {"username": setting(conn, "admin_user"), "version": VERSION,
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
            elif parts in (["configs"], ["chains"]):
                handler = {"GET": self.api_configs, "POST": self.api_config_save}.get(method)
            elif len(parts) == 2 and parts[0] in ("configs", "chains") and parts[1].isdigit():
                handler = {"PATCH": self.api_config_save, "DELETE": self.api_chain_del}.get(method)
            elif parts == ["routes"]:
                handler = {"GET": self.api_routes, "POST": self.api_route_save}.get(method)
            elif len(parts) == 2 and parts[0] == "routes" and parts[1].isdigit():
                handler = {"PATCH": self.api_route_save, "DELETE": self.api_route_del}.get(method)
            elif parts == ["tunnels"]:
                handler = {"GET": self.api_tunnels, "POST": self.api_tunnel_save}.get(method)
            elif len(parts) == 2 and parts[0] == "tunnels" and parts[1].isdigit():
                handler = {"PATCH": self.api_tunnel_save, "DELETE": self.api_tunnel_del}.get(method)
            elif len(parts) == 2 and parts[0] in KINDS and parts[1] == "order":
                handler = {"POST": self.api_order}.get(method)
                parts = [parts[0]]   # 表名作为参数传入
            elif parts == ["groups"]:
                handler = {"POST": self.api_group_add}.get(method)
            elif len(parts) == 2 and parts[0] == "groups" and parts[1].isdigit():
                handler = {"PATCH": self.api_group_edit, "DELETE": self.api_group_del}.get(method)
            elif parts == ["settings"]:
                handler = {"GET": self.api_settings, "PATCH": self.api_settings_save}.get(method)
            if not handler:
                return self.send_json(404, {"error": "接口不存在"})

            args = [int(parts[1])] if len(parts) > 1 else ([parts[0]] if handler == self.api_order else [])
            self.query = query
            if method != "GET":
                conn.execute("BEGIN IMMEDIATE")
            with conn:
                result = handler(conn, body, *args)
            self.send_json(200, result)
        except UserError as e:
            self.send_json(e.status, {"error": str(e), "field": e.field})
        finally:
            conn.close()

    def body_length(self, limit):
        lengths = self.headers.get_all("Content-Length", [])
        value = lengths[0].strip() if lengths else "0"
        try:
            if (len(lengths) > 1 or self.headers.get("Transfer-Encoding") is not None
                    or not re.fullmatch(r"[0-9]+", value)):
                raise ValueError
            length = int(value)
        except ValueError:
            self.close_connection = True
            raise UserError("请求格式错误")
        if length > limit:
            self.close_connection = True
            raise UserError("请求过大")
        return length

    def read_json(self):
        length = self.body_length(MAX_API_BODY)
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
        return {"id": n["id"], "kind": n["kind"], "name": n["name"], "note": n["note"], "addr": n["addr"],
                "ports": n["ports"], "group_id": n["group_id"], "status": node_status(conn, n, now),
                "last_seen": n["last_seen"]}

    def api_nodes(self, conn, body):
        now = int(time.time())
        kind = self.query.get("kind", ["forward"])[0]
        table = node_table(kind)
        nodes = [{**self.node_brief(conn, n, now), "bar": node_bar(conn, n, now)}
                 for n in conn.execute(ordered(table)).fetchall()]
        return {"now": now, "kind": kind, "groups": get_groups(conn, table), "nodes": nodes}

    def api_node_add(self, conn, body):
        name = check_remark(body.get("name"), "name", "名称")
        note = check_note(body.get("note"))
        addr = check_host(body.get("addr"), "addr")
        ports = format_ports(parse_ports(body.get("ports", "10000-65535")))
        if "heartbeat_interval" in body or "tunnel_heartbeat" in body:
            raise UserError("心跳间隔只能在系统设置中全局配置")
        kind = body.get("kind", "forward")
        table = node_table(kind)
        group_id = check_group(conn, table, body.get("group_id"))
        with conn:
            node_id = int(setting(conn, "next_node_id"))
            set_setting(conn, "next_node_id", str(node_id + 1))
            cur = conn.execute(
                f"""INSERT INTO {table} (id, name, note, addr, key, ports, group_id, kind, sort)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, (SELECT COALESCE(MAX(sort), 0) + 1 FROM {table}))""",
                (node_id, name, note, addr, new_key(), ports, group_id, kind))
            add_event(conn, cur.lastrowid, f"创建节点，入站地址 {addr}")
        return {"id": cur.lastrowid}

    def api_node_get(self, conn, body, node_id):
        now = int(time.time())
        n = get_node(conn, node_id)
        # 下发给节点的规则按规则 id 排序（影响版本号），这里只调整显示顺序
        chain_sort = {r["id"]: i for i, r in enumerate(conn.execute(ordered("chains")))}
        rules = sorted(node_rule_rows(conn, n), key=lambda r: (chain_sort[r["chain_id"]], r["id"]))
        chains = conn.execute(
            ordered("chains", "WHERE t.id IN (SELECT chain_id FROM hops WHERE node_id = ?)"), (node_id,)).fetchall()
        group = conn.execute("SELECT name FROM item_groups WHERE id = ?", (n["group_id"],)).fetchone()
        events = conn.execute(
            "SELECT ts, text FROM events WHERE node_id = ? ORDER BY ts DESC, id DESC LIMIT ?",
            (node_id, EVENTS_SHOWN)).fetchall()
        return {
            "now": now,
            "sync_path": setting(conn, "sync_path"),
            "public_url": setting(conn, "public_url", ""),
            "agent_url": AGENT_URL,
            "version": VERSION,
            "forward_version": FORWARD_VERSION,
            "tunnel_version": TUNNEL_VERSION,
            "tunnel_path": setting(conn, "tunnel_path"),
            "tunnel_installer_url": f"https://github.com/{REPO}/releases/download/v{VERSION}/install-tunnel.sh",
            "heartbeat_interval": heartbeat_interval(conn),
            "node": {
                **self.node_brief(conn, n, now), "bar": node_bar(conn, n, now), "key": n["key"],
                "first_seen": n["first_seen"], "src_ip": n["src_ip"], "skew": n["skew"], "agent": n["agent"],
                "core_version": n["core_version"],
                "reported_v": n["reported_v"], "want_v": tunnel_snapshot(conn, n)["revision"] if n["kind"] == "tunnel" else rules_version(node_rules(conn, n)),
                "state": n["state"], "err": n["err"], "group": group["name"],
                "agent_update": agent_update(n["agent"], n["auto_update"]),
            },
            "rules": rules,
            "chains": [{"id": c["id"], "remark": c["remark"]} for c in chains],
            "events": [dict(e) for e in events],
        }

    def api_node_edit(self, conn, body, node_id):
        n = get_node(conn, node_id)
        table = node_table(n["kind"])
        if "heartbeat_interval" in body or "tunnel_heartbeat" in body:
            raise UserError("心跳间隔只能在系统设置中全局配置")
        if "kind" in body and body["kind"] != n["kind"]:
            raise UserError("节点类型不能修改", "kind")
        name = check_remark(body["name"], "name", "名称") if "name" in body else n["name"]
        note = check_note(body["note"]) if "note" in body else n["note"]
        addr = check_host(body["addr"], "addr") if "addr" in body else n["addr"]
        ranges = parse_ports(body["ports"]) if "ports" in body else parse_ports(n["ports"])
        group_id = check_group(conn, table, body["group_id"]) if "group_id" in body else n["group_id"]
        outside = [r for r in conn.execute(
            """SELECT c.remark, h.port FROM hops h JOIN chains c ON c.id = h.chain_id
               WHERE h.node_id = ? ORDER BY h.port""", (node_id,)) if not in_ports(r["port"], ranges)]
        for r in conn.execute("SELECT name AS remark, port FROM tunnels WHERE node_id = ? UNION ALL SELECT '等待移除' AS remark, port FROM retired_ports WHERE node_id = ?", (node_id, node_id)):
            if not in_ports(r["port"], ranges):
                outside.append(r)
        if outside:
            used = "、".join(f"「{r['remark']}」{r['port']}" for r in outside)
            raise UserError(f"以下转发使用的端口不在新的允许范围内：{used}。请先修改这些转发。", "ports")
        key = None
        with conn:
            conn.execute(f"UPDATE {table} SET name = ?, note = ?, addr = ?, ports = ? WHERE id = ?",
                         (name, note, addr, format_ports(ranges), node_id))
            if group_id != n["group_id"]:   # 换到其他分组时排在该组末尾
                move_group(conn, table, node_id, group_id)
            if addr != n["addr"]:
                key = new_key()
                conn.execute(f"UPDATE {table} SET key = ? WHERE id = ?", (key, node_id))
                add_event(conn, node_id, f"入站地址 {n['addr']} → {addr}，已重新生成密钥")
                for t in conn.execute("SELECT id FROM tunnels WHERE node_id = ?", (node_id,)).fetchall():
                    conn.execute("UPDATE chains SET target_host = ? WHERE tunnel_id = ?", (addr, t["id"]))
                    for c in conn.execute("SELECT id FROM chains WHERE tunnel_id = ?", (t["id"],)).fetchall():
                        mark_config_nodes(conn, c["id"])
                for r in conn.execute(
                        """SELECT h.chain_id, h.node_id FROM hops h
                           JOIN hops h2 ON h2.chain_id = h.chain_id AND h2.seq = h.seq + 1
                           WHERE h2.node_id = ?""", (node_id,)).fetchall():
                    mark_pending(conn, r["chain_id"], [r["node_id"]])
        return {"key": key, "upstream": upstream_nodes(conn, node_id) if key else []}

    def api_node_rekey(self, conn, body, node_id):
        table = node_table(get_node(conn, node_id)["kind"])
        key = new_key()
        with conn:
            conn.execute(f"UPDATE {table} SET key = ? WHERE id = ?", (key, node_id))
            add_event(conn, node_id, "重置密钥")
        return {"key": key}

    def api_node_del(self, conn, body, node_id):
        n = get_node(conn, node_id)
        table = node_table(n["kind"])
        used = [r[0] for r in conn.execute(
            """SELECT DISTINCT c.remark FROM hops h JOIN chains c ON c.id = h.chain_id
               WHERE h.node_id = ? ORDER BY c.id""", (node_id,))]
        for r in conn.execute("SELECT name, node_ids FROM routes"):
            if node_id in json.loads(r["node_ids"]):
                used.append("链路：" + r["name"])
        used.extend("隧道：" + t[0] for t in conn.execute("SELECT name FROM tunnels WHERE node_id = ?", (node_id,)))
        if used:
            raise UserError(f"节点仍被转发使用：{'、'.join(used)}。请先删除或修改这些转发。")
        force = self.query.get("force") == ["1"]
        with conn:
            if force or not n["first_seen"]:
                delete_node(conn, node_id)
                return {"deleted": True}
            if not n["deleting"]:
                conn.execute(f"UPDATE {table} SET deleting = 1 WHERE id = ?", (node_id,))
                add_event(conn, node_id, "开始删除，等待节点清空规则")
        return {"deleted": False}

    # 转发

    def api_chains(self, conn, body):
        now = int(time.time())
        nodes = {n["id"]: n for n in conn.execute("SELECT * FROM nodes").fetchall()}
        status = {nid: node_status(conn, n, now) for nid, n in nodes.items()}
        node_info = lambda nid: {"node_id": nid, "name": nodes[nid]["name"], "status": status[nid]}
        chains = []
        for c in conn.execute(ordered("chains")).fetchall():
            hops = [{**node_info(h["node_id"]), "addr": nodes[h["node_id"]]["addr"], "port": h["port"]}
                    for h in conn.execute("SELECT * FROM hops WHERE chain_id = ? ORDER BY seq", (c["id"],))]
            pending = [node_info(r["node_id"]) for r in conn.execute(
                "SELECT node_id FROM chain_pending WHERE chain_id = ? ORDER BY node_id", (c["id"],))]
            chains.append({"id": c["id"], "remark": c["remark"], "target_host": c["target_host"],
                           "target_port": c["target_port"], "deleting": bool(c["deleting"]),
                           "group_id": c["group_id"], "hops": hops, "pending": pending})
        # 节点及其分组用于筛选下拉框
        node_list = [{"id": n["id"], "name": n["name"], "group_id": n["group_id"]}
                     for n in conn.execute(ordered("nodes")).fetchall()]
        return {"chains": chains, "groups": get_groups(conn, "chains"),
                "nodes": node_list, "node_groups": get_groups(conn, "nodes")}

    def api_chain_del(self, conn, body, chain_id):
        chain = get_chain(conn, chain_id)
        force = self.query.get("force") == ["1"]
        with conn:
            if force:
                for h in conn.execute("SELECT node_id, port FROM hops WHERE chain_id = ?", (chain_id,)).fetchall():
                    reserve_removed(conn, h["node_id"], h["port"])
                delete_chain(conn, chain_id)
                return {"deleted": True}
            # 保留之前被移出、尚未确认的节点；只等待仍存在且连接过的节点。
            conn.execute("""DELETE FROM chain_pending WHERE chain_id = ? AND node_id NOT IN
                            (SELECT id FROM nodes WHERE first_seen IS NOT NULL)""", (chain_id,))
            if not chain["deleting"]:
                conn.execute("UPDATE chains SET deleting = 1 WHERE id = ?", (chain_id,))
                mark_pending(conn, chain_id, [r[0] for r in conn.execute(
                    """SELECT h.node_id FROM hops h JOIN nodes n ON n.id = h.node_id
                       WHERE h.chain_id = ? AND n.first_seen IS NOT NULL""", (chain_id,))])
            finish_deleted_chains(conn)
            deleted = not conn.execute("SELECT 1 FROM chains WHERE id = ?", (chain_id,)).fetchone()
        return {"deleted": deleted}

    # 排序

    def api_order(self, conn, body, kind):
        """保存顺序：groups 为分组顺序，ids 为全部对象按显示顺序排列（只能在组内调整，不改变所属分组）。"""
        groups, ids = body.get("groups"), body.get("ids")
        existing_groups = {r[0] for r in conn.execute("SELECT id FROM item_groups WHERE kind = ?", (kind,))}
        existing = {r[0] for r in conn.execute(f"SELECT id FROM {kind}")}
        if (not isinstance(groups, list) or not isinstance(ids, list)
                or any(type(x) is not int for x in groups + ids)
                or len(groups) != len(existing_groups) or set(groups) != existing_groups
                or len(ids) != len(existing) or set(ids) != existing):
            raise UserError("列表已变化或排序范围不匹配，请刷新后重试")
        with conn:
            for i, gid in enumerate(groups):
                conn.execute("UPDATE item_groups SET sort = ? WHERE id = ?", (i + 1, gid))
            for i, item_id in enumerate(ids):
                conn.execute(f"UPDATE {kind} SET sort = ? WHERE id = ?", (i + 1, item_id))
        return {}

    def get_group(self, conn, group_id):
        g = conn.execute("SELECT * FROM item_groups WHERE id = ?", (group_id,)).fetchone()
        if not g:
            raise UserError("分组不存在", status=404)
        return g

    def api_group_add(self, conn, body):
        kind = body.get("kind")
        if kind not in KINDS:
            raise UserError("请求格式错误")
        name = check_remark(body.get("name"), "name", "分组名称")
        with conn:
            cur = conn.execute(
                """INSERT INTO item_groups (kind, name, sort)
                   VALUES (?, ?, (SELECT COALESCE(MAX(sort), 0) + 1 FROM item_groups WHERE kind = ?))""",
                (kind, name, kind))
        return {"id": cur.lastrowid}

    def api_group_edit(self, conn, body, group_id):
        self.get_group(conn, group_id)
        name = check_remark(body.get("name"), "name", "分组名称")
        with conn:
            conn.execute("UPDATE item_groups SET name = ? WHERE id = ?", (name, group_id))
        return {}

    def api_group_del(self, conn, body, group_id):
        """删除分组，组内对象按原顺序移到默认分组末尾。"""
        g = self.get_group(conn, group_id)
        if g["builtin"]:
            raise UserError("默认分组不能删除")
        kind = g["kind"]
        with conn:
            base = conn.execute(f"SELECT COALESCE(MAX(sort), 0) FROM {kind}").fetchone()[0]
            conn.execute(f"UPDATE {kind} SET group_id = ?, sort = sort + ? WHERE group_id = ?",
                         (default_group(conn, kind), base, group_id))
            conn.execute("DELETE FROM item_groups WHERE id = ?", (group_id,))
        return {}

    # 0.2.0：链路模板、组合配置、隧道及独立 Agent 协议。

    def api_routes(self, conn, body):
        result = []
        for r in conn.execute(ordered("routes")):
            ids = json.loads(r["node_ids"])
            refs = [dict(c) for c in conn.execute("SELECT id, remark FROM chains WHERE route_id = ? ORDER BY id", (r["id"],))]
            result.append({"id": r["id"], "name": r["name"], "group_id": r["group_id"], "node_ids": ids, "configs": refs,
                           "nodes": [{"id": nid, "name": get_node(conn, nid)["name"]} for nid in ids]})
        return {"routes": result, "groups": get_groups(conn, "routes")}

    def api_route_save(self, conn, body, route_id=None):
        old = get_route(conn, route_id) if route_id is not None else None
        name = check_remark(body.get("name", old["name"] if old else None), "name", "名称")
        ids = body.get("node_ids", json.loads(old["node_ids"]) if old else None)
        if not isinstance(ids, list) or not 1 <= len(ids) <= 32 or any(type(n) is not int for n in ids):
            raise UserError("链路需要 1-32 个有序的转发节点", "node_ids")
        for nid in ids:
            n = get_node(conn, nid)
            if n["kind"] != "forward" or n["deleting"]:
                raise UserError("链路只能使用未删除的转发节点", "node_ids")
        group_id = check_group(conn, "routes", body.get("group_id", old["group_id"] if old else None))
        refs = conn.execute("SELECT * FROM chains WHERE route_id = ? AND deleting = 0 ORDER BY id", (route_id,)).fetchall()
        with conn:
            if old:
                conn.execute("UPDATE routes SET name = ?, node_ids = ? WHERE id = ?", (name, json.dumps(ids), route_id))
                if group_id != old["group_id"]:
                    move_group(conn, "routes", route_id, group_id)
            else:
                route_id = conn.execute("INSERT INTO routes(name, node_ids, group_id, sort) VALUES (?, ?, ?, (SELECT COALESCE(MAX(sort), 0) + 1 FROM routes))", (name, json.dumps(ids), group_id)).lastrowid
            if old and ids != json.loads(old["node_ids"]):
                for c in refs:
                    before = [r[0] for r in conn.execute("SELECT node_id FROM hops WHERE chain_id = ?", (c["id"],))]
                    pairs, _ = route_hops(conn, get_route(conn, route_id), c["id"])
                    apply_config_hops(conn, c["id"], pairs)
                    mark_pending(conn, c["id"], before + [nid for nid, _ in pairs])
        return {"id": route_id, "affected": len(refs)}

    def api_route_del(self, conn, body, route_id):
        get_route(conn, route_id)
        if conn.execute("SELECT 1 FROM chains WHERE route_id = ?", (route_id,)).fetchone():
            raise UserError("链路仍被配置引用，请先删除或修改引用它的配置")
        with conn:
            conn.execute("DELETE FROM routes WHERE id = ?", (route_id,))
        return {"deleted": True}

    def api_configs(self, conn, body):
        data = self.api_chains(conn, body)
        for c in data["chains"]:
            row = get_chain(conn, c["id"])
            c.update(route_id=row["route_id"], route_name=get_route(conn, row["route_id"])["name"] if row["route_id"] else None,
                     tunnel_id=row["tunnel_id"], target_type="tunnel" if row["tunnel_id"] else "direct",
                     ready=bool(row["ready"]), tunnel_name=None, connection=None)
            if row["tunnel_id"]:
                t = get_tunnel(conn, row["tunnel_id"])
                external = t["source"] == "external"
                n = None if external else get_node(conn, t["node_id"])
                c["tunnel_name"] = t["name"]
                c["tunnel_source"] = t["source"]
                c["tunnel_status"] = self.tunnel_brief(conn, t)["status"]
                c["tunnel_agent_status"] = None if external else node_status(conn, n, int(time.time()))
                if not c["deleting"]:
                    first = c["hops"][0] if c["hops"] else {
                        "addr": t["external_host"] if external else n["addr"], "port": t["port"]}
                    c["connection"] = tunnel_config.connection(t, first["addr"], first["port"], c["remark"])
                    if not row["route_id"]:
                        c["connection"].pop("tcp_only", None)
            elif c["hops"] and not c["deleting"]:
                first = c["hops"][0]
                c["connection"] = {"protocol": "direct", "host": first["addr"], "port": first["port"],
                                   "uri": f"{first['addr']}:{first['port']}"}
            c["status"] = ("deleting" if c["deleting"] else
                           "error" if ((row["tunnel_id"] and c["tunnel_status"] == "error") or
                                       any(p["status"] in ("error", "skew") for p in c["pending"])) else
                           "waiting" if not c["ready"] else "applying" if c["pending"] else
                           "saved" if c.get("tunnel_source") == "external" and not c["hops"] else "applied")
        return data

    def api_config_save(self, conn, body, chain_id=None):
        old = get_chain(conn, chain_id) if chain_id is not None else None
        if old and old["deleting"]:
            raise UserError("配置正在删除，不能修改")
        remark = check_remark(body.get("remark"))
        route_id = body.get("route_id")
        target_type = body.get("target_type", "direct")
        route = get_route(conn, route_id) if route_id is not None else None
        if route is None and target_type != "tunnel":
            raise UserError("IP/端口目标必须选择链路", "route_id")
        if route is None and body.get("ports") not in (None, []):
            raise UserError("直连隧道不分配转发端口", "ports")
        pairs, assigned = route_hops(conn, route, chain_id, body.get("ports")) if route else ([], [])
        tunnel_id, ready = None, 1
        if target_type == "tunnel":
            t = get_tunnel(conn, body.get("tunnel_id"))
            n = None if t["source"] == "external" else get_node(conn, t["node_id"])
            if t["deleting"] or (n is not None and n["deleting"]):
                raise UserError("隧道正在删除", "tunnel_id")
            tunnel_id, host, port = t["id"], t["external_host"] if n is None else n["addr"], t["port"]
            ready = 1 if n is None else int(tunnel_ready(conn, n["id"]))
        elif target_type == "direct":
            host = check_host(body.get("target_host"), "target_host")
            port = check_port(body.get("target_port"), "target_port")
        else:
            raise UserError("目标类型无效", "target_type")
        group_id = check_group(conn, "chains", body.get("group_id", old["group_id"] if old else None))
        before = [(h["node_id"], h["port"]) for h in conn.execute("SELECT * FROM hops WHERE chain_id = ? ORDER BY seq", (chain_id,))]
        with conn:
            if not old:
                chain_id = conn.execute("""INSERT INTO chains(remark, target_host, target_port, group_id, route_id, tunnel_id, ready, sort)
                    VALUES (?, ?, ?, ?, ?, ?, ?, (SELECT COALESCE(MAX(sort), 0) + 1 FROM chains))""",
                    (remark, host, port, group_id, route_id, tunnel_id, ready)).lastrowid
            else:
                conn.execute("""UPDATE chains SET remark = ?, target_host = ?, target_port = ?, group_id = ?,
                    route_id = ?, tunnel_id = ?, ready = ? WHERE id = ?""",
                    (remark, host, port, group_id, route_id, tunnel_id, ready, chain_id))
            if old and group_id != old["group_id"]:
                move_group(conn, "chains", chain_id, group_id)
            if before != pairs:
                apply_config_hops(conn, chain_id, pairs)
            if not old or before != pairs or (old["target_host"], old["target_port"], old["ready"]) != (host, port, ready):
                mark_pending(conn, chain_id, [nid for nid, _ in before + pairs])
        return {"id": chain_id, "assigned": assigned}

    def tunnel_brief(self, conn, t):
        if t["source"] == "external":
            return {"id": t["id"], "name": t["name"], "group_id": t["group_id"], "node_id": None,
                    "node_name": "手动导入", "source": "external", "host": t["external_host"],
                    "protocol": t["protocol"], "port": t["port"], "deleting": False, "status": "imported",
                    "agent_status": None, "params": json.loads(t["params"]),
                    "uri": tunnel_config.connection(t, t["external_host"], t["port"], t["name"])["uri"],
                    "references": conn.execute("SELECT COUNT(*) FROM chains WHERE tunnel_id = ?", (t["id"],)).fetchone()[0]}
        n = get_node(conn, t["node_id"])
        want = tunnel_snapshot(conn, n)["revision"]
        state = "deleting" if t["deleting"] else "applied" if tunnel_ready(conn, n["id"]) else "error" if n["state"] == "error" else "applying"
        # 凭据只在已认证的面板中显示，私钥只下发给对应 Agent。
        p = json.loads(t["params"])
        p.pop("private_key", None)
        return {"id": t["id"], "name": t["name"], "group_id": t["group_id"], "node_id": t["node_id"], "node_name": n["name"], "source": "agent",
                "protocol": t["protocol"], "port": t["port"], "params": p, "deleting": bool(t["deleting"]),
                "status": state, "agent_status": node_status(conn, n, int(time.time())),
                "want_v": want, "err": n["err"] or "", "references": conn.execute(
                    "SELECT COUNT(*) FROM chains WHERE tunnel_id = ?", (t["id"],)).fetchone()[0]}

    def api_tunnels(self, conn, body):
        return {"tunnels": [self.tunnel_brief(conn, t) for t in conn.execute(ordered("tunnels"))],
                "groups": get_groups(conn, "tunnels")}

    def api_tunnel_save(self, conn, body, tunnel_id=None):
        old = get_tunnel(conn, tunnel_id) if tunnel_id is not None else None
        if old and old["deleting"]:
            raise UserError("隧道正在删除")
        source = body.get("source", old["source"] if old else "agent")
        if source not in ("agent", "external") or (old and source != old["source"]):
            raise UserError("隧道来源无效或与原记录不一致", "source")
        if source == "external":
            return self.api_imported_tunnel_save(conn, body, old)
        name = check_remark(body.get("name"), "name", "名称")
        nid = body.get("node_id", old["node_id"] if old else None)
        n = get_node(conn, nid)
        if n["kind"] != "tunnel" or n["deleting"]:
            raise UserError("请选择可用的隧道节点", "node_id")
        protocol = body.get("protocol", old["protocol"] if old else None)
        if protocol not in ("shadowsocks", "vless-reality", "socks5"):
            raise UserError("协议不受支持", "protocol")
        if old and (nid != old["node_id"] or protocol != old["protocol"]):
            raise UserError("不能修改已有隧道的节点或协议，请新建隧道")
        used = {r[0] for r in conn.execute("SELECT port FROM tunnels WHERE node_id = ? AND id IS NOT ?", (nid, tunnel_id))}
        used |= {r[0] for r in conn.execute("SELECT port FROM retired_ports WHERE node_id = ?", (nid,))}
        port = body.get("port", old["port"] if old else None)
        port = check_port(port, "port") if str(port or "").strip() else pick_port(parse_ports(n["ports"]), used)
        if port is None or not in_ports(port, parse_ports(n["ports"])):
            raise UserError("没有可用端口，或端口不在节点允许范围内", "port")
        if port in used:
            raise UserError("端口已占用或尚在等待删除确认", "port")
        group_id = check_group(conn, "tunnels", body.get("group_id", old["group_id"] if old else None))
        params = json.loads(old["params"]) if old else tunnel_config.new_credentials(protocol)
        supplied = body.get("params", {})
        if not isinstance(supplied, dict):
            raise UserError("协议参数格式错误", "params")
        if protocol == "vless-reality":
            params["server_name"] = check_host(supplied.get("server_name", params.get("server_name")), "server_name")
            params["handshake_host"] = check_host(supplied.get("handshake_host", params.get("handshake_host", params["server_name"])), "handshake_host")
            params["handshake_port"] = check_port(supplied.get("handshake_port", params.get("handshake_port", 443)), "handshake_port")
        if protocol == "socks5":
            for key in ("username", "password"):
                if supplied.get(key):
                    value = str(supplied[key])
                    if not 1 <= len(value.encode()) <= 255 or any(ord(ch) < 32 for ch in value):
                        raise UserError("用户名和密码需为 1-255 字节且不能包含控制字符", key)
                    params[key] = value
        with conn:
            if not old:
                tunnel_id = conn.execute("INSERT INTO tunnels(name, node_id, protocol, port, params, group_id, sort) VALUES (?, ?, ?, ?, ?, ?, (SELECT COALESCE(MAX(sort), 0) + 1 FROM tunnels))",
                    (name, nid, protocol, port, tunnel_config.canonical(params), group_id)).lastrowid
            else:
                changed = old["port"] != port or json.loads(old["params"]) != params
                if old["port"] != port:
                    reserve_removed(conn, nid, old["port"])
                conn.execute("UPDATE tunnels SET name = ?, port = ?, params = ? WHERE id = ?",
                             (name, port, tunnel_config.canonical(params), tunnel_id))
                if group_id != old["group_id"]:
                    move_group(conn, "tunnels", tunnel_id, group_id)
                if changed:
                    block_tunnel_configs(conn, tunnel_id)
                    conn.execute("UPDATE chains SET target_port = ? WHERE tunnel_id = ?", (port, tunnel_id))
        return {"id": tunnel_id, "port": port}

    def api_imported_tunnel_save(self, conn, body, old):
        if any(key in body for key in ("node_id", "port", "protocol", "params")):
            raise UserError("手动导入隧道的地址、协议和凭据请通过分享链接修改", "uri")
        if "uri" in body or not old:
            try:
                parsed = tunnel_config.import_uri(body.get("uri"))
                host = check_host(parsed["host"], "uri")
                if parsed["protocol"] == "vless-reality":
                    check_host(parsed["params"]["server_name"], "uri")
            except ValueError as e:
                raise UserError(str(e), "uri") from None
            port, protocol, params = parsed["port"], parsed["protocol"], tunnel_config.canonical(parsed["params"])
            fallback_name = old["name"] if old else parsed["name"]
        else:
            host, port, protocol, params = old["external_host"], old["port"], old["protocol"], old["params"]
            fallback_name = old["name"]
        name = check_remark(body.get("name") or fallback_name, "name", "名称")
        group_id = check_group(conn, "tunnels", body.get("group_id", old["group_id"] if old else None))
        with conn:
            if old:
                tunnel_id = old["id"]
                conn.execute("""UPDATE tunnels SET name=?,external_host=?,port=?,protocol=?,params=? WHERE id=?""",
                             (name, host, port, protocol, params, tunnel_id))
                if group_id != old["group_id"]:
                    move_group(conn, "tunnels", tunnel_id, group_id)
                if (host, port) != (old["external_host"], old["port"]):
                    conn.execute("UPDATE chains SET target_host=?,target_port=? WHERE tunnel_id=?", (host, port, tunnel_id))
                    for c in conn.execute("SELECT id FROM chains WHERE tunnel_id=? AND deleting=0", (tunnel_id,)).fetchall():
                        mark_config_nodes(conn, c["id"])
            else:
                tunnel_id = conn.execute("""INSERT INTO tunnels(name,protocol,port,params,group_id,sort,source,external_host)
                    VALUES (?,?,?,?,?,(SELECT COALESCE(MAX(sort),0)+1 FROM tunnels),'external',?)""",
                    (name, protocol, port, params, group_id, host)).lastrowid
        return {"id": tunnel_id, "port": port, "source": "external"}

    def api_tunnel_del(self, conn, body, tunnel_id):
        t = get_tunnel(conn, tunnel_id)
        if conn.execute("SELECT 1 FROM chains WHERE tunnel_id = ?", (tunnel_id,)).fetchone():
            raise UserError("隧道仍被配置引用，请先解除引用")
        with conn:
            if t["source"] == "external" or not get_node(conn, t["node_id"])["first_seen"]:
                conn.execute("DELETE FROM tunnels WHERE id = ?", (tunnel_id,))
                return {"deleted": True}
            conn.execute("UPDATE tunnels SET deleting = 1 WHERE id = ?", (tunnel_id,))
        return {"deleted": False}

    def tunnel_api(self, method, path):
        conn = db()
        try:
            peer = ipaddress.ip_address(self.client_address[0])
            if self.headers.get("X-Forwarded-Proto") != "https" or not (peer.is_private or peer.is_loopback):
                # 明文仅允许显式本地开发；生产 Agent 也强制验证 HTTPS。
                if not (os.environ.get("ICTL_ALLOW_LOCAL_HTTP") == "1" and peer.is_loopback):
                    raise UserError("隧道 Agent 接口需要 HTTPS", status=403)
            authorization = self.headers.get("Authorization", "")
            key = authorization[7:] if authorization.startswith("Bearer ") else ""
            node = next((n for n in conn.execute("SELECT * FROM tunnel_nodes").fetchall()
                         if hmac.compare_digest(n["key"], key)), None)
            if node is None:
                self.close_connection = True
                return self.not_found()
            if method == "GET" and path == "events":
                return self.tunnel_events(conn, node)
            if method == "GET" and path == "config":
                return self.send_json(200, tunnel_snapshot(conn, node))
            if method == "GET" and path == "components":
                manifest = json.loads(json.dumps(COMPONENTS))
                manifest["agent_assets"] = {}
                for arch in ("amd64", "arm64"):
                    filename = os.path.join(BASE_DIR, "assets", f"tunnel-agent-linux-{arch}")
                    if os.path.isfile(filename):
                        with open(filename, "rb") as f:
                            manifest["agent_assets"][arch] = {"sha256": hashlib.sha256(f.read()).hexdigest(), "path": f"download/{arch}"}
                return self.send_json(200, manifest)
            if method == "GET" and path in ("download/amd64", "download/arm64"):
                filename = os.path.join(BASE_DIR, "assets", "tunnel-agent-linux-" + path.split("/")[1])
                if not os.path.isfile(filename):
                    return self.not_found()
                with open(filename, "rb") as f:
                    return self.reply(200, f.read(), "application/octet-stream", [("Cache-Control", "no-store")])
            if method != "POST" or path != "heartbeat":
                return self.not_found()
            if not self.headers.get("Content-Type", "").startswith("application/json"):
                raise UserError("请求格式错误")
            body = self.read_json()
            if body.get("api") != 1 or body.get("state") not in ("init", "ok", "error"):
                raise UserError("不兼容的 Agent 协议")
            now = int(time.time())
            with conn:
                conn.execute("BEGIN IMMEDIATE")
                node = get_node(conn, node["id"])
                if not hmac.compare_digest(node["key"], key):
                    return self.not_found()
                until = now + 3 * heartbeat_interval(conn)
                last = conn.execute("SELECT start, end FROM tunnel_presence WHERE node_id = ? ORDER BY start DESC LIMIT 1", (node["id"],)).fetchone()
                if last and last["end"] >= now:
                    conn.execute("UPDATE tunnel_presence SET end = ? WHERE node_id = ? AND start = ?", (until, node["id"], last["start"]))
                else:
                    conn.execute("INSERT INTO tunnel_presence VALUES (?, ?, ?)", (node["id"], now, until))
                    add_event(conn, node["id"], "Agent 首次连接" if not node["first_seen"] else "Agent 恢复在线", now)
                state = body["state"]
                report = str(body.get("revision", ""))[:64]
                err = str(body.get("error", ""))[:500]
                agent = str(body.get("agent_version", ""))[:32]
                core = str(body.get("core_version", ""))[:32]
                if state == "error" and (node["state"] != state or node["err"] != err):
                    add_event(conn, node["id"], "配置应用失败：" + err, now)
                conn.execute("""UPDATE tunnel_nodes SET first_seen = COALESCE(first_seen, ?), last_seen = ?, online_until = ?,
                    src_ip = ?, reported_v = ?, state = ?, err = ?, agent = ?, core_version = ? WHERE id = ?""",
                    (now, now, until, self.source_ip(), report, state, err, agent, core, node["id"]))
                want = tunnel_snapshot(conn, node)["revision"]
                removed = False
                if state == "ok" and report == want:
                    conn.execute("DELETE FROM retired_ports WHERE node_id = ?", (node["id"],))
                    conn.execute("DELETE FROM tunnels WHERE node_id = ? AND deleting = 1", (node["id"],))
                    for c in conn.execute("""SELECT c.id FROM chains c JOIN tunnels t ON t.id = c.tunnel_id
                        WHERE t.node_id = ? AND c.ready = 0 AND c.deleting = 0""", (node["id"],)).fetchall():
                        conn.execute("UPDATE chains SET ready = 1 WHERE id = ?", (c["id"],))
                        mark_config_nodes(conn, c["id"])
                    if node["deleting"]:
                        delete_node(conn, node["id"])
                        removed = True
            return self.send_json(200, {"api": 1, "revision": want, "heartbeat_interval": heartbeat_interval(conn),
                                       "deleted": removed, "agent_version": TUNNEL_VERSION})
        except UserError as e:
            self.close_connection = True
            self.send_json(e.status, {"error": str(e), "field": e.field})
        finally:
            conn.close()

    def tunnel_events(self, conn, node):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache, no-store")
        self.send_header("X-Accel-Buffering", "no")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        self.connection.settimeout(20)
        last, keepalive = None, 0
        # 定期重连释放线程，逐轮验证节点和密钥，重置凭据会关闭旧订阅。
        deadline = time.monotonic() + 300
        try:
            while time.monotonic() < deadline:
                n = conn.execute("SELECT * FROM tunnel_nodes WHERE id = ?", (node["id"],)).fetchone()
                if not n or not hmac.compare_digest(n["key"], node["key"]):
                    break
                revision = tunnel_snapshot(conn, n)["revision"]
                if revision != last:
                    data = json.dumps({"revision": revision})
                    self.wfile.write(f"event: config\ndata: {data}\n\n".encode())
                    last = revision
                    self.wfile.flush()
                if time.monotonic() >= keepalive:
                    self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
                    keepalive = time.monotonic() + 15
                time.sleep(1)
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            pass

    # 设置

    def api_settings(self, conn, body):
        return {"public_url": setting(conn, "public_url", ""), "sync_path": setting(conn, "sync_path"),
                "panel_path": setting(conn, "panel_path"), "username": setting(conn, "admin_user"),
                "version": VERSION, "components": COMPONENTS, "tunnel_path": setting(conn, "tunnel_path"),
                "tunnel_heartbeat": heartbeat_interval(conn)}

    def api_settings_save(self, conn, body):
        url = str(body.get("public_url", setting(conn, "public_url", ""))).strip().rstrip("/")
        if url and not re.fullmatch(r"https?://(?:[A-Za-z0-9.-]+|\[[0-9a-fA-F:]+\])(?::[0-9]{1,5})?", url):
            raise UserError("格式应为 http(s)://域名或IP[:端口]，不带路径", "public_url")
        interval = body.get("tunnel_heartbeat", heartbeat_interval(conn))
        if type(interval) is not int or not 5 <= interval <= 3600:
            raise UserError("心跳间隔须为 5-3600 秒的整数", "tunnel_heartbeat")
        with conn:
            now = int(time.time())
            if interval != heartbeat_interval(conn):
                conn.execute("UPDATE tunnel_nodes SET online_until = MAX(online_until, ? + ?) WHERE online_until >= ?", (now, 3 * interval, now))
                conn.execute("UPDATE tunnel_presence SET end = MAX(end, ? + ?) WHERE end >= ?", (now, 3 * interval, now))
            set_setting(conn, "tunnel_heartbeat", str(interval))
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
    load_agent()
    Handler.sync_path = setting(conn, "sync_path")
    Handler.panel_path = setting(conn, "panel_path")
    Handler.tunnel_path = setting(conn, "tunnel_path")
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
