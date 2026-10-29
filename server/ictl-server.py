#!/usr/bin/env python3
"""ictl 服务端：节点同步接口 + Web 管理面板。

数据库路径默认 /var/lib/ictl-server/ictl.db，可用环境变量 ICTL_DB 覆盖。
"""

import argparse
import base64
import hashlib
import hmac
import http.client
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
import target_config

VERSION = "0.3.0"
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
with open(os.path.join(BASE_DIR, "components.json")) as _f:
    COMPONENTS = json.load(_f)
FORWARD_VERSION = COMPONENTS["forward_agent"]
REPO = "moran1024a/iptables-control"
AGENT_URL = f"https://github.com/{REPO}/releases/download/v{VERSION}/ictl.sh"
DB_PATH = os.environ.get("ICTL_DB", "/var/lib/ictl-server/ictl.db")
WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")
# 同版本的节点端脚本，供节点自动更新时下载：Docker 镜像中为 /app/agent/ictl.sh，源码目录中为 ../agent/ictl.sh
AGENT_FILES = [os.path.join(os.path.dirname(os.path.abspath(__file__)), p)
               for p in ("agent/ictl.sh", "../agent/ictl.sh")]
AGENT_AUTO_UPDATE = (0, 1, 7)    # 从该版本起，节点端按服务端的更新信号自动更新

OFFLINE_AFTER = 120         # 超过该秒数没有上报视为离线（主动节点每轮等待 5-10 秒）
MAX_SKEW = 60               # 节点时间允许的最大偏差（秒）
MAX_BODY = 8192             # 同步请求体上限
MAX_API_BODY = 65536        # 面板请求体上限
WINDOW = 60                 # 被动活动与旧版状态记录：每格 60 秒
ACTIVE_WINDOW = 20          # 主动状态条：每格 20 秒，覆盖多次 5–10 秒轮询
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
KINDS = ("nodes", "routes", "targets", "chains")
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


def init_legacy_db():
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


# Version 202 is retained only as a migration baseline; runtime uses targets.
TARGET_SCHEMA = """
CREATE TABLE targets (
 id INTEGER PRIMARY KEY, name TEXT NOT NULL, type TEXT NOT NULL,
 host TEXT NOT NULL, port INTEGER NOT NULL, params TEXT NOT NULL DEFAULT '{}',
 group_id INTEGER NOT NULL, sort INTEGER NOT NULL DEFAULT 0,
 CHECK(type IN ('endpoint','shadowsocks','vless-reality')),
 CHECK(port BETWEEN 1 AND 65535)
);
"""
CHAIN_SCHEMA = """CREATE TABLE chains (
 id INTEGER PRIMARY KEY, remark TEXT NOT NULL, target_id INTEGER NOT NULL REFERENCES targets(id),
 route_id INTEGER REFERENCES routes(id), deleting INTEGER NOT NULL DEFAULT 0,
 sort INTEGER NOT NULL DEFAULT 0, group_id INTEGER NOT NULL DEFAULT 0
)"""
SYNC_COLUMNS = {
    "desired_mode": "TEXT NOT NULL DEFAULT 'active' CHECK(desired_mode IN ('active','passive'))",
    "reported_mode": "TEXT", "desired_generation": "INTEGER NOT NULL DEFAULT 1",
    "applied_generation": "INTEGER NOT NULL DEFAULT 0", "manual_required": "INTEGER NOT NULL DEFAULT 0",
    "protocol_version": "INTEGER NOT NULL DEFAULT 1", "last_manual": "INTEGER", "last_success": "INTEGER",
}
SYNC_SCHEMA = """
CREATE TABLE sync_ops (
 node_id INTEGER PRIMARY KEY, op TEXT NOT NULL, source TEXT NOT NULL,
 generation INTEGER NOT NULL, mode TEXT NOT NULL, hash TEXT NOT NULL,
 rules TEXT NOT NULL, expires INTEGER NOT NULL, result TEXT
);
CREATE TABLE deleted_receipts (
 key TEXT PRIMARY KEY, op TEXT NOT NULL, generation INTEGER NOT NULL, mode TEXT NOT NULL,
 hash TEXT NOT NULL, expires INTEGER NOT NULL
);
"""


def add_sync_schema(conn):
    for col, ddl in SYNC_COLUMNS.items():
        conn.execute(f"ALTER TABLE nodes ADD COLUMN {col} {ddl}")
    conn.execute("ALTER TABLE windows ADD COLUMN manual_count INTEGER NOT NULL DEFAULT 0")
    conn.execute("ALTER TABLE windows ADD COLUMN last_activity INTEGER")
    ensure_active_windows(conn)
    for statement in SYNC_SCHEMA.split(';'):
        if statement.strip():
            conn.execute(statement)


def ensure_active_windows(conn):
    # Keep minute-level history intact; it cannot reconstruct individual 20-second samples.
    conn.execute("""CREATE TABLE IF NOT EXISTS active_windows (
        node_id INTEGER NOT NULL, win INTEGER NOT NULL, level INTEGER NOT NULL,
        PRIMARY KEY(node_id, win))""")
    if setting(conn, "active_window_start") is None:
        set_setting(conn, "active_window_start", str(int(time.time())))


def backup_database(conn, path):
    # Do not replace an earlier successful backup, including a WAL-consistent one.
    if os.path.exists(path):
        return
    temporary = path + '.tmp'
    backup = sqlite3.connect(temporary)
    try:
        conn.backup(backup)
    finally:
        backup.close()
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


def init_db():
    os.makedirs(os.path.dirname(os.path.abspath(DB_PATH)), mode=0o700, exist_ok=True)
    conn = db()
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version > 300:
        conn.close()
        raise UserError("数据库版本高于本服务端，请使用兼容版本")
    if version == 300:
        with conn:
            conn.execute("BEGIN IMMEDIATE")
            ensure_active_windows(conn)
        return conn
    exists = conn.execute("SELECT 1 FROM sqlite_master WHERE name='nodes'").fetchone()
    if not exists:
        conn.execute("PRAGMA journal_mode=WAL")
        # Build the current schema directly. SCHEMA is the archived 0.1 baseline.
        schema = SCHEMA.replace(SCHEMA[SCHEMA.index('CREATE TABLE IF NOT EXISTS chains'):SCHEMA.index('CREATE TABLE IF NOT EXISTS hops')],
                                TARGET_SCHEMA + CHAIN_SCHEMA + ';\n')
        conn.executescript("BEGIN IMMEDIATE;\n" + schema)
        with conn:
            conn.execute("CREATE TABLE routes(id INTEGER PRIMARY KEY,name TEXT NOT NULL,node_ids TEXT NOT NULL,group_id INTEGER NOT NULL,sort INTEGER NOT NULL DEFAULT 0)")
            conn.execute("CREATE TABLE retired_ports(node_id INTEGER NOT NULL,port INTEGER NOT NULL,PRIMARY KEY(node_id,port))")
            conn.execute("ALTER TABLE nodes ADD COLUMN kind TEXT NOT NULL DEFAULT 'forward' CHECK(kind='forward')")
            add_sync_schema(conn)
            for kind in KINDS:
                conn.execute("INSERT INTO item_groups(kind,name,builtin) VALUES (?,?,1)", (kind, DEFAULT_GROUP))
            for k, v in (("sync_path", '/' + secrets.token_hex(12)), ("panel_path", '/' + secrets.token_hex(8)),
                         ("window", str(WINDOW)), ("next_node_id", '1'), ("next_hop_id", '1')):
                set_setting(conn, k, v)
            reset_admin(conn)
            conn.execute("PRAGMA user_version=300")
        return conn
    backup_database(conn, DB_PATH + '.pre-0.3.0.bak')
    if version < 202:
        conn.close()
        conn = init_legacy_db()
    try:
        migrate_v030(conn)
    except Exception:
        conn.close()
        raise
    return conn


def migrate_v030(conn):
    """Delete only managed tunnel data; preserve external credentials and forwarding IDs."""
    managed = {r[0] for r in conn.execute("SELECT id FROM tunnels WHERE source='agent'")}
    removed = {r[0] for r in conn.execute("SELECT id FROM chains WHERE tunnel_id IN (SELECT id FROM tunnels WHERE source='agent')")}
    old_nodes = conn.execute("SELECT * FROM nodes").fetchall()
    before = {n['id']: legacy_node_rules(conn, n) for n in old_nodes}
    expected = {n['id']: [f"{r['id']} both {r['port']} {r['host']} {r['tport']}"
                         for r in legacy_node_rule_rows(conn, n) if r['chain_id'] not in removed] for n in old_nodes}
    tunnel_count = conn.execute("SELECT COUNT(*) FROM tunnel_nodes").fetchone()[0]
    conn.execute("PRAGMA foreign_keys=OFF")
    try:
        with conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(TARGET_SCHEMA)
            conn.execute("UPDATE item_groups SET kind='targets' WHERE kind='tunnels'")
            conn.execute("""INSERT INTO targets(id,name,type,host,port,params,group_id,sort)
                SELECT id,name,protocol,external_host,port,params,group_id,sort FROM tunnels WHERE source='external'""")
            external_count = conn.execute("SELECT COUNT(*) FROM targets").fetchone()[0]
            for cid in removed:
                for h in conn.execute("SELECT * FROM hops WHERE chain_id=?", (cid,)).fetchall():
                    reserve_removed(conn, h['node_id'], h['port'])
                delete_chain(conn, cid)
            conn.execute(CHAIN_SCHEMA.replace('CREATE TABLE chains', 'CREATE TABLE chains_new'))
            for c in conn.execute("SELECT * FROM chains ORDER BY id").fetchall():
                tid = c['tunnel_id']
                if tid is None:
                    tid = conn.execute("INSERT INTO targets(name,type,host,port,group_id,sort) VALUES (?,'endpoint',?,?,?,?)",
                        (c['remark'], c['target_host'], c['target_port'], default_group(conn, 'targets'), c['sort'])).lastrowid
                conn.execute("INSERT INTO chains_new(id,remark,target_id,route_id,deleting,sort,group_id) VALUES (?,?,?,?,?,?,?)",
                             (c['id'], c['remark'], tid, c['route_id'], c['deleting'], c['sort'], c['group_id']))
            conn.execute("DROP TABLE chains")
            conn.execute("ALTER TABLE chains_new RENAME TO chains")
            conn.execute("DROP VIEW all_nodes")
            for table in ('windows', 'events', 'chain_pending', 'retired_ports'):
                conn.execute(f"DELETE FROM {table} WHERE node_id IN (SELECT id FROM tunnel_nodes)")
            for table in ('tunnels', 'tunnel_presence', 'tunnel_nodes'):
                conn.execute(f"DROP TABLE {table}")
            conn.execute("DELETE FROM item_groups WHERE kind='tunnel_nodes'")
            conn.execute("DELETE FROM settings WHERE k IN ('tunnel_path','tunnel_heartbeat')")
            for column in ('core_version', 'online_until'):
                conn.execute(f"ALTER TABLE nodes DROP COLUMN {column}")
            add_sync_schema(conn)
            for n in old_nodes:
                lines = node_rules(conn, n)
                if lines != expected[n['id']]:
                    raise UserError("升级校验失败：保留配置的规则发生变化")
                applied = int(before[n['id']] == lines and n['state'] == 'ok' and n['reported_v'] == rules_version(lines))
                conn.execute("UPDATE nodes SET applied_generation=? WHERE id=?", (applied, n['id']))
            if conn.execute("PRAGMA foreign_key_check").fetchall():
                raise UserError("升级校验失败：引用不完整")
            conn.execute("PRAGMA user_version=300")
    finally:
        conn.execute("PRAGMA foreign_keys=ON")
    print(f"0.3.0 迁移完成：删除隧道节点 {tunnel_count}、受管隧道 {len(managed)}、关联配置 {len(removed)}；"
          f"保留外部目标 {external_count}。备份：{DB_PATH}.pre-0.3.0.bak；远端隧道服务请自行卸载。", flush=True)


def read_db():
    conn = sqlite3.connect('file:' + os.path.abspath(DB_PATH) + '?mode=ro', uri=True, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def mutation_snapshot(conn):
    out = {}
    for n in conn.execute('SELECT * FROM nodes').fetchall():
        out[n['id']] = (dict(n), node_rule_rows(conn, n),
                       {r[0] for r in conn.execute('SELECT port FROM retired_ports WHERE node_id=?', (n['id'],))})
    return out


def track_mutation(method):
    """Keep generation changes and all affected configuration waits in the same transaction."""
    def wrapped(self, conn, *args, **kwargs):
        with conn:
            if not conn.in_transaction:
                conn.execute('BEGIN IMMEDIATE')
            before = mutation_snapshot(conn)
            result = method(self, conn, *args, **kwargs)
            after = mutation_snapshot(conn)
            for nid, (n, rows, retired) in after.items():
                old, old_rows, old_retired = before.get(nid, ({}, [], set()))
                # Remarks are display-only; compare the actual wire rules per configuration.
                key = lambda r: (r['id'], r['port'], r['host'], r['tport'])
                changed = ([key(r) for r in rows] != [key(r) for r in old_rows] or retired != old_retired or
                           any(n[k] != old.get(k, n[k]) for k in ('key','desired_mode','deleting')))
                if changed:
                    conn.execute('UPDATE nodes SET desired_generation=desired_generation+1 WHERE id=?', (nid,))
                for cid in {r['chain_id'] for r in old_rows + rows}:
                    if [key(r) for r in old_rows if r['chain_id']==cid] != [key(r) for r in rows if r['chain_id']==cid]:
                        if conn.execute('SELECT 1 FROM chains WHERE id=?', (cid,)).fetchone():
                            mark_pending(conn, cid, [nid])
            return result
    return wrapped


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
    node = conn.execute("SELECT * FROM nodes WHERE id = ?", (node_id,)).fetchone()
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
    conn.execute("DELETE FROM sync_ops WHERE node_id = ?", (node_id,))
    conn.execute(f"DELETE FROM {table} WHERE id = ?", (node_id,))
    conn.execute("DELETE FROM windows WHERE node_id = ?", (node_id,))
    conn.execute("DELETE FROM active_windows WHERE node_id = ?", (node_id,))
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
        conn.execute("DELETE FROM active_windows WHERE win < ?", ((now - RETENTION) // ACTIVE_WINDOW,))
        conn.execute("DELETE FROM events WHERE ts < ?", (now - RETENTION,))
        conn.execute("DELETE FROM sessions WHERE expires < ?", (now,))
        conn.execute("DELETE FROM sync_ops WHERE expires < ?", (now - RETENTION,))
        conn.execute("DELETE FROM deleted_receipts WHERE expires < ?", (now,))


def migrate_v020(conn):
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version > 200:
        raise UserError("数据库版本高于本服务端，请使用兼容版本")
    if version == 200:
        return
    # 物理表 chains/hops 继续保存实际配置，避免重编号或改变规则顺序。
    before = {n["id"]: legacy_node_rules(conn, n) for n in conn.execute("SELECT * FROM nodes")}
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
        after = {n["id"]: legacy_node_rules(conn, n) for n in conn.execute("SELECT * FROM nodes")}
        if before != after:
            raise UserError("升级校验失败：转发规则发生变化，迁移已回滚")
        conn.execute("PRAGMA user_version = 200")


def migrate_layout(conn):
    """拆分节点存储与五类分组；不重编号节点、凭据、转发规则。"""
    original = [dict(n) for n in conn.execute("SELECT * FROM nodes ORDER BY id")]
    before = {n["id"]: legacy_node_rules(conn, n) for n in original if n["kind"] == "forward"}
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
            after = {n["id"]: legacy_node_rules(conn, n) for n in conn.execute("SELECT * FROM nodes")}
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
    if kind != "forward":
        raise UserError("只支持 iptables 转发节点", "kind")
    return "nodes"


def move_group(conn, table, item_id, group_id):
    """换组后排在新组末尾，不改变业务配置或下发版本。"""
    conn.execute(f"""UPDATE {table} SET group_id = ?, sort =
                 (SELECT COALESCE(MAX(sort), 0) + 1 FROM {table} WHERE group_id = ?)
                 WHERE id = ?""", (group_id, group_id, item_id))


def get_route(conn, route_id):
    if type(route_id) is not int or route_id <= 0:
        raise UserError("无效的对象 ID", "route_id")
    r = conn.execute("SELECT * FROM routes WHERE id = ?", (route_id,)).fetchone()
    if not r:
        raise UserError("链路不存在", "route_id", 404)
    return r


def get_target(conn, target_id):
    if type(target_id) is not int or target_id <= 0:
        raise UserError("请选择目标", "target_id")
    target = conn.execute("SELECT * FROM targets WHERE id = ?", (target_id,)).fetchone()
    if not target:
        raise UserError("目标不存在", "target_id", 404)
    return target


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

def legacy_node_rule_rows(conn, node):
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


def legacy_node_rules(conn, node):
    return [f"{r['id']} both {r['port']} {r['host']} {r['tport']}" for r in legacy_node_rule_rows(conn, node)]


def node_rule_rows(conn, node):
    """节点应有的规则：非最后一跳指向下一跳节点，最后一跳指向链的目标。"""
    if node["deleting"]:
        return []
    rows = conn.execute(
        f"""
        SELECT h.id, h.port, c.id AS chain_id, c.remark AS chain_remark, t.host AS target_host, t.port AS target_port,
               n2.addr AS next_addr, h2.port AS next_port
        FROM hops h
        JOIN chains c ON c.id = h.chain_id
        JOIN targets t ON t.id = c.target_id
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
    if node["manual_required"] or node["desired_mode"] == "passive":
        return "syncing" if (node["manual_required"] or node["applied_generation"] != node["desired_generation"]
                              or node["state"] != "ok") else "synced"
    if not node["first_seen"]:
        return "new"
    if now - node["last_seen"] > OFFLINE_AFTER:
        return "offline"
    if node["skew"] is not None and abs(node["skew"]) > MAX_SKEW:
        return "skew"
    if node["state"] == "error":
        return "error"
    if node["applied_generation"] != node["desired_generation"] or node["reported_v"] != rules_version(node_rules(conn, node)) or node["state"] == "init":
        return "syncing"
    return "synced" if node["state"] == "ok" else "error"


def node_bar(conn, node, now):
    """最近 BAR_WINDOWS 个时间窗口的状态：ok / fault / lost / none。"""
    if node["desired_mode"] == "passive":
        start = now // WINDOW - BAR_WINDOWS + 1
        activity = {r["win"]: dict(r) for r in conn.execute(
            "SELECT win,manual_count,last_activity FROM windows WHERE node_id=? AND win>=?", (node["id"], start))}
        return {"start": start * WINDOW, "window": WINDOW,
                "levels": ["ok" if activity.get(w, {}).get("manual_count", 0) else "white" for w in range(start, now // WINDOW + 1)],
                "counts": [activity.get(w, {}).get("manual_count", 0) for w in range(start, now // WINDOW + 1)],
                "times": [activity.get(w, {}).get("last_activity") for w in range(start, now // WINDOW + 1)]}
    cur = now // ACTIVE_WINDOW
    start = cur - BAR_WINDOWS + 1
    levels = {r[0]: r[1] for r in conn.execute(
        "SELECT win, level FROM active_windows WHERE node_id = ? AND win >= ?", (node["id"], start))}
    recording_since = int(setting(conn, "active_window_start", "0"))
    first = node["first_seen"]
    bar = []
    for w in range(start, cur + 1):
        if w in levels:
            bar.append("ok" if levels[w] == 2 else "fault")
        elif (not first or (w + 1) * ACTIVE_WINDOW <= first or w == cur
              or w * ACTIVE_WINDOW < recording_since):
            bar.append("none")
        else:
            bar.append("lost")
    return {"start": start * ACTIVE_WINDOW, "window": ACTIVE_WINDOW, "levels": bar}


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
    elif node["desired_mode"] == "active" and now - node["last_seen"] > OFFLINE_AFTER:
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
        """INSERT INTO active_windows(node_id,win,level) VALUES (?, ?, ?)
           ON CONFLICT (node_id, win) DO UPDATE SET level = MIN(level, excluded.level)""",
        (node["id"], now // ACTIVE_WINDOW, 2 if health == "ok" else 1),
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


def clear_pending(conn, node):
    conn.execute('DELETE FROM retired_ports WHERE node_id=?', (node['id'],))
    conn.execute('DELETE FROM chain_pending WHERE node_id=?', (node['id'],))
    finish_deleted_chains(conn)


def sync_report(conn, node, fields, source_ip, now=None):
    """Return authenticated plaintext. Network IO is handled outside the DB transaction."""
    now = int(time.time()) if now is None else now
    state = fields.get('state')
    if state not in ('init', 'ok', 'error') or not re.fullmatch(r'[0-9]{1,12}', fields.get('t', '')):
        raise UserError('无效上报')
    skew = int(fields['t']) - now
    err, agent = fields.get('err', '')[:500], fields.get('agent', '')[:32]
    health = 'skew' if abs(skew) > MAX_SKEW else 'error' if state == 'error' else 'ok'
    if health == 'skew':
        conn.execute('UPDATE nodes SET skew=?,health=? WHERE id=?', (skew, health, node['id']))
        return f'time={now}\n'
    proto = fields.get('proto') == '2'
    source, phase = fields.get('source'), fields.get('phase')
    if proto and (source not in ('manual','auto') or phase not in ('begin','poll','ack','asset')):
        raise UserError('无效同步阶段')
    au = {'0': 0, '1': 1}.get(fields.get('au'))
    # Asset downloads never acknowledge state, register activity, or mutate operation context.
    if fields.get('get') == 'agent':
        if AGENT and au and (not node['manual_required'] and node['desired_mode']=='active' or proto and source=='manual'):
            return AGENT[2]
        raise UserError('没有可用更新')
    record_report(conn, node, now, health, err, skew)
    conn.execute('''UPDATE nodes SET first_seen=COALESCE(first_seen,?),last_seen=?,src_ip=?,
        agent=?,skew=?,health=?,auto_update=?,protocol_version=MAX(protocol_version,?) WHERE id=?''',
        (now, now, source_ip, agent, skew, health, au, 2 if proto else 1, node['id']))
    if proto and fields.get('mode') in ('active','passive'):
        conn.execute('UPDATE nodes SET reported_mode=? WHERE id=?', (fields['mode'], node['id']))
    lines = node_rules(conn, node)
    want = rules_version(lines)
    update = f'update={AGENT[0]} {AGENT[1]}\n' if agent_update(agent, au)=='pending' else ''
    if not proto:
        if node['protocol_version'] >= 2 or node['manual_required'] or node['desired_mode'] != 'active':
            return 'same\n' + update  # Legacy requests cannot clear v2 waits.
        v = fields.get('v', '')
        conn.execute('UPDATE nodes SET reported_v=?,state=?,err=? WHERE id=?', (v, state, err, node['id']))
        if v == want and state == 'ok':
            conn.execute('UPDATE nodes SET applied_generation=desired_generation WHERE id=?', (node['id'],))
            clear_pending(conn, node)
            if node['deleting']:
                delete_node(conn, node['id'])
                return 'same\n'
        return ('same\n' if v == want and state != 'init' else '\n'.join([f'v={want}']+lines)+'\n') + update
    if source == 'auto' and (node['desired_mode'] == 'passive' or node['manual_required']):
        return 'proto=2\naction=pause\nmode=passive\n'
    opid = fields.get('op', '')
    if not re.fullmatch(r'[a-f0-9]{32}', opid):
        raise UserError('无效操作编号')
    op = conn.execute('SELECT * FROM sync_ops WHERE node_id=?', (node['id'],)).fetchone()
    if phase == 'ack':
        if (not op or op['op'] != opid or op['source'] != source or op['expires'] < now or
                fields.get('generation') != str(op['generation']) or fields.get('mode') != op['mode'] or fields.get('v') != op['hash']):
            return 'proto=2\nresult=expired\n'
        if op['result']:
            return 'proto=2\nresult='+op['result']+'\n'
        current = (op['generation'] == node['desired_generation'] and op['mode'] == node['desired_mode'] and op['hash'] == want)
        result = 'failed' if state != 'ok' else 'confirmed' if current else 'changed'
        conn.execute('UPDATE sync_ops SET result=? WHERE node_id=?', (result, node['id']))
        conn.execute('UPDATE nodes SET state=?,err=? WHERE id=?', (state, err, node['id']))
        if state == 'ok':
            conn.execute('UPDATE nodes SET reported_v=?,reported_mode=?,applied_generation=?,last_success=? WHERE id=?',
                         (op['hash'], op['mode'], op['generation'], now, node['id']))
        add_event(conn, node['id'], ('手动' if source=='manual' else '自动') + '同步：' +
                  {'confirmed':'确认成功','changed':'已应用旧快照，仍有新变更','failed':'失败：'+err}[result], now)
        if result == 'confirmed':
            if source == 'manual':
                conn.execute('UPDATE nodes SET manual_required=0 WHERE id=?', (node['id'],))
            clear_pending(conn, node)
            if node['deleting']:
                conn.execute('INSERT OR REPLACE INTO deleted_receipts VALUES (?,?,?,?,?,?)',
                             (node['key'], opid, op['generation'], op['mode'], op['hash'], now+600))
                delete_node(conn, node['id'])
                return 'proto=2\nresult=deleted\n'
        return f'proto=2\nresult={result}\n'
    if phase not in ('begin','poll') or (source=='manual' and phase!='begin') or (source=='auto' and phase!='poll'):
        raise UserError('无效同步阶段')
    if source == 'auto' and op and op['source']=='manual' and not op['result'] and op['expires'] >= now:
        return 'proto=2\naction=busy\n'
    if op and op['op']==opid:
        if op['expires'] < now or op['source'] != source:
            return 'proto=2\nresult=expired\n'
    else:
        if source == 'auto' and (node['applied_generation']==node['desired_generation'] and
                fields.get('generation')==str(node['desired_generation']) and fields.get('v')==want and state=='ok'):
            conn.execute("UPDATE nodes SET state='ok',err='' WHERE id=?", (node['id'],))
            return 'proto=2\naction=same\n' + update
        op = {'op':opid, 'source':source, 'generation':node['desired_generation'], 'mode':node['desired_mode'],
              'hash':want, 'rules':'\n'.join(lines), 'expires':now+600}
        conn.execute('''INSERT OR REPLACE INTO sync_ops(node_id,op,source,generation,mode,hash,rules,expires)
            VALUES (?,?,?,?,?,?,?,?)''', (node['id'], opid, source, op['generation'], op['mode'], want, op['rules'], op['expires']))
        if source == 'manual':
            conn.execute('''INSERT INTO windows(node_id,win,level,manual_count,last_activity) VALUES (?,?,2,1,?)
                ON CONFLICT(node_id,win) DO UPDATE SET manual_count=manual_count+1,last_activity=excluded.last_activity''',
                         (node['id'], now//WINDOW, now))
            conn.execute("UPDATE nodes SET last_manual=?,state='init' WHERE id=?", (now,node['id']))
            add_event(conn, node['id'], '手动同步开始', now)
    return (f"proto=2\naction=apply\nop={opid}\ngeneration={op['generation']}\nmode={op['mode']}\n"
            f"v={op['hash']}\n{op['rules']}\n" + update)


class LimitedHTTPServer(http.server.ThreadingHTTPServer):
    daemon_threads = True
    max_connections = 64

    def __init__(self, *args, **kwargs):
        self.slots = threading.BoundedSemaphore(self.max_connections)
        super().__init__(*args, **kwargs)

    def process_request(self, request, client_address):
        if not self.slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self.slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    sync_path = "/"
    panel_path = "/"
    timeout = 15

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
        try:
            length = self.body_length(MAX_BODY)
            raw = base64.b64decode(self.rfile.read(length).strip(), validate=True)
            if len(raw) < 40:
                raise ValueError()
        except (UserError, ValueError):
            return self.not_found()
        msg, req_tag = raw[:-32], raw[-32:]
        conn = db()
        out = None
        try:
            # Authentication/decryption happens before obtaining the SQLite writer lock.
            identities = [dict(n) for n in conn.execute('SELECT * FROM nodes')]
            identities += [dict(r) for r in conn.execute('SELECT * FROM deleted_receipts WHERE expires>=?', (int(time.time()),))]
            for candidate in identities:
                ek, mk = derive(candidate['key'])
                if hmac.compare_digest(req_tag, mac(mk, msg)):
                    break
            else:
                return self.not_found()
            plaintext = aes(ek, b'Salted__'+msg, decrypt=True).decode('utf-8')
            fields = {}
            for line in plaintext.splitlines():
                k, sep, v = line.partition('=')
                if sep:
                    if k in fields:
                        raise UserError('重复字段')
                    fields[k] = v.strip()
            with conn:
                conn.execute('BEGIN IMMEDIATE')
                node = conn.execute('SELECT * FROM nodes WHERE key=?', (candidate['key'],)).fetchone()
                if node:
                    out = sync_report(conn, node, fields, self.source_ip())
                else:
                    receipt = conn.execute('SELECT * FROM deleted_receipts WHERE key=? AND expires>=?',
                                           (candidate['key'], int(time.time()))).fetchone()
                    if (receipt and fields.get('proto')=='2' and fields.get('phase')=='ack' and fields.get('state')=='ok'
                            and fields.get('op')==receipt['op'] and fields.get('v')==receipt['hash']
                            and fields.get('generation')==str(receipt['generation']) and fields.get('mode')==receipt['mode']
                            and fields.get('t','').isdigit() and abs(int(fields['t'])-int(time.time()))<=MAX_SKEW):
                        out = 'proto=2\nresult=deleted\n'
        except (UserError, ValueError, UnicodeError):
            pass
        finally:
            conn.close()
        if out is None:
            return self.not_found()
        data = out if isinstance(out, bytes) else out.encode()
        self.reply(200, seal_bytes(candidate['key'], req_tag, data))

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
            elif parts == ["targets"]:
                handler = {"GET": self.api_targets, "POST": self.api_target_save}.get(method)
            elif len(parts) == 2 and parts[0] == "targets" and parts[1].isdigit():
                handler = {"PATCH": self.api_target_save, "DELETE": self.api_target_del}.get(method)
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
                "last_seen": n["last_seen"], "desired_mode": n["desired_mode"], "reported_mode": n["reported_mode"],
                "manual_required": bool(n["manual_required"]), "protocol_version": n["protocol_version"],
                "desired_generation": n["desired_generation"], "applied_generation": n["applied_generation"],
                "err": n["err"]}

    def api_nodes(self, conn, body):
        now = int(time.time())
        kind = self.query.get("kind", ["forward"])[0]
        table = node_table(kind)
        nodes = [{**self.node_brief(conn, n, now), "bar": node_bar(conn, n, now)}
                 for n in conn.execute(ordered(table)).fetchall()]
        return {"now": now, "kind": kind, "groups": get_groups(conn, table), "nodes": nodes}

    @track_mutation
    def api_node_add(self, conn, body):
        name = check_remark(body.get("name"), "name", "名称")
        note = check_note(body.get("note"))
        addr = check_host(body.get("addr"), "addr")
        ports = format_ports(parse_ports(body.get("ports", "10000-65535")))
        if "heartbeat_interval" in body or "tunnel_heartbeat" in body:
            raise UserError("新版只支持主动／被动同步模式")
        kind = body.get("kind", "forward")
        table = node_table(kind)
        group_id = check_group(conn, table, body.get("group_id"))
        node_id = int(setting(conn, "next_node_id"))
        set_setting(conn, "next_node_id", str(node_id + 1))
        cur = conn.execute(
            f"""INSERT INTO {table} (id, name, note, addr, key, ports, group_id, kind, sort)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, (SELECT COALESCE(MAX(sort), 0) + 1 FROM {table}))""",
            (node_id, name, note, addr, new_key(), ports, group_id, kind))
        add_event(conn, cur.lastrowid, f"创建节点，入站地址 {addr}")
        mode = body.get("desired_mode", "active")
        if mode not in ("active", "passive"):
            raise UserError("模式必须为主动或被动", "desired_mode")
        if mode == "passive":
            conn.execute("UPDATE nodes SET desired_mode='passive',manual_required=1 WHERE id=?", (cur.lastrowid,))
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
            "node": {
                **self.node_brief(conn, n, now), "bar": node_bar(conn, n, now), "key": n["key"],
                "first_seen": n["first_seen"], "src_ip": n["src_ip"], "skew": n["skew"], "agent": n["agent"],
                "reported_v": n["reported_v"], "want_v": rules_version(node_rules(conn, n)),
                "state": n["state"], "err": n["err"], "group": group["name"],
                "agent_update": agent_update(n["agent"], n["auto_update"]),
            },
            "rules": rules,
            "chains": [{"id": c["id"], "remark": c["remark"]} for c in chains],
            "events": [dict(e) for e in events],
        }

    @track_mutation
    def api_node_edit(self, conn, body, node_id):
        n = get_node(conn, node_id)
        table = node_table(n["kind"])
        if "heartbeat_interval" in body or "tunnel_heartbeat" in body:
            raise UserError("新版只支持主动／被动同步模式")
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
        for r in conn.execute("SELECT '等待移除' AS remark, port FROM retired_ports WHERE node_id = ?", (node_id,)):
            if not in_ports(r["port"], ranges):
                outside.append(r)
        if outside:
            used = "、".join(f"「{r['remark']}」{r['port']}" for r in outside)
            raise UserError(f"以下转发使用的端口不在新的允许范围内：{used}。请先修改这些转发。", "ports")
        mode = body.get("desired_mode", n["desired_mode"])
        if mode not in ("active", "passive"):
            raise UserError("模式必须为主动或被动", "desired_mode")
        if mode != n["desired_mode"] and n["first_seen"] and n["protocol_version"] < 2:
            raise UserError("请先升级节点 Agent 并完成一次同步，再切换模式", "desired_mode")
        key = None
        if mode != n["desired_mode"]:
            conn.execute("UPDATE nodes SET desired_mode=?,manual_required=1 WHERE id=?", (mode, node_id))
            add_event(conn, node_id, "同步模式已修改，等待本机手动同步确认")
        conn.execute(f"UPDATE {table} SET name = ?, note = ?, addr = ?, ports = ? WHERE id = ?",
                     (name, note, addr, format_ports(ranges), node_id))
        if group_id != n["group_id"]:   # 换到其他分组时排在该组末尾
            move_group(conn, table, node_id, group_id)
        if addr != n["addr"]:
            key = new_key()
            conn.execute(f"UPDATE {table} SET key = ? WHERE id = ?", (key, node_id))
            add_event(conn, node_id, f"入站地址 {n['addr']} → {addr}，已重新生成密钥")
            for r in conn.execute(
                    """SELECT h.chain_id, h.node_id FROM hops h
                       JOIN hops h2 ON h2.chain_id = h.chain_id AND h2.seq = h.seq + 1
                       WHERE h2.node_id = ?""", (node_id,)).fetchall():
                mark_pending(conn, r["chain_id"], [r["node_id"]])
        return {"key": key, "upstream": upstream_nodes(conn, node_id) if key else []}

    @track_mutation
    def api_node_rekey(self, conn, body, node_id):
        table = node_table(get_node(conn, node_id)["kind"])
        key = new_key()
        conn.execute(f"UPDATE {table} SET key = ? WHERE id = ?", (key, node_id))
        add_event(conn, node_id, "重置密钥")
        return {"key": key}

    @track_mutation
    def api_node_del(self, conn, body, node_id):
        n = get_node(conn, node_id)
        table = node_table(n["kind"])
        used = [r[0] for r in conn.execute(
            """SELECT DISTINCT c.remark FROM hops h JOIN chains c ON c.id = h.chain_id
               WHERE h.node_id = ? ORDER BY c.id""", (node_id,))]
        for r in conn.execute("SELECT name, node_ids FROM routes"):
            if node_id in json.loads(r["node_ids"]):
                used.append("链路：" + r["name"])
        if used:
            raise UserError(f"节点仍被转发使用：{'、'.join(used)}。请先删除或修改这些转发。")
        force = self.query.get("force") == ["1"]
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
        node_info = lambda nid: {"node_id": nid, "name": nodes[nid]["name"], "status": status[nid],
                                 "desired_mode": nodes[nid]["desired_mode"], "err": nodes[nid]["err"]}
        chains = []
        for c in conn.execute(ordered("chains")).fetchall():
            hops = [{**node_info(h["node_id"]), "addr": nodes[h["node_id"]]["addr"], "port": h["port"]}
                    for h in conn.execute("SELECT * FROM hops WHERE chain_id = ? ORDER BY seq", (c["id"],))]
            pending = [node_info(r["node_id"]) for r in conn.execute(
                "SELECT node_id FROM chain_pending WHERE chain_id = ? ORDER BY node_id", (c["id"],))]
            target = get_target(conn, c["target_id"])
            chains.append({"id": c["id"], "remark": c["remark"], "target_host": target["host"],
                           "target_port": target["port"], "deleting": bool(c["deleting"]),
                           "group_id": c["group_id"], "hops": hops, "pending": pending})
        # 节点及其分组用于筛选下拉框
        node_list = [{"id": n["id"], "name": n["name"], "group_id": n["group_id"]}
                     for n in conn.execute(ordered("nodes")).fetchall()]
        return {"chains": chains, "groups": get_groups(conn, "chains"),
                "nodes": node_list, "node_groups": get_groups(conn, "nodes")}

    @track_mutation
    def api_chain_del(self, conn, body, chain_id):
        chain = get_chain(conn, chain_id)
        force = self.query.get("force") == ["1"]
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

    # 链路模板、目标对象与组合配置。

    def api_routes(self, conn, body):
        result = []
        for r in conn.execute(ordered("routes")):
            ids = json.loads(r["node_ids"])
            refs = [dict(c) for c in conn.execute("SELECT id, remark FROM chains WHERE route_id = ? ORDER BY id", (r["id"],))]
            result.append({"id": r["id"], "name": r["name"], "group_id": r["group_id"], "node_ids": ids, "configs": refs,
                           "nodes": [{"id": nid, "name": get_node(conn, nid)["name"]} for nid in ids]})
        return {"routes": result, "groups": get_groups(conn, "routes")}

    @track_mutation
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
        return {"id": route_id, "affected": len(refs)}

    @track_mutation
    def api_route_del(self, conn, body, route_id):
        get_route(conn, route_id)
        if conn.execute("SELECT 1 FROM chains WHERE route_id = ?", (route_id,)).fetchone():
            raise UserError("链路仍被配置引用，请先删除或修改引用它的配置")
        conn.execute("DELETE FROM routes WHERE id = ?", (route_id,))
        return {"deleted": True}

    def api_configs(self, conn, body):
        data = self.api_chains(conn, body)
        for c in data["chains"]:
            row = get_chain(conn, c["id"])
            t = get_target(conn, row["target_id"])
            c.update(route_id=row["route_id"], route_name=get_route(conn, row["route_id"])["name"] if row["route_id"] else None,
                     target_id=t["id"], target_name=t["name"], connection=None)
            if not c["deleting"]:
                first = c["hops"][0] if c["hops"] else {"addr": t["host"], "port": t["port"]}
                c["connection"] = target_config.connection(t, first["addr"], first["port"], c["remark"])
            c["status"] = ("deleting" if c["deleting"] else "applying" if c["pending"] else
                           "saved" if not c["hops"] else "applied")
        return data

    @track_mutation
    def api_config_save(self, conn, body, chain_id=None):
        old = get_chain(conn, chain_id) if chain_id is not None else None
        if old and old["deleting"]:
            raise UserError("配置正在删除，不能修改")
        if any(k in body for k in ("target_host", "target_port", "tunnel_id", "target_type")):
            raise UserError("配置只能引用目标对象", "target_id")
        remark = check_remark(body.get("remark", old["remark"] if old else None))
        route_id = body.get("route_id", old["route_id"] if old else None)
        route = get_route(conn, route_id) if route_id is not None else None
        target = get_target(conn, body.get("target_id", old["target_id"] if old else None))
        if route is None and body.get("ports") not in (None, []):
            raise UserError("直连目标不分配转发端口", "ports")
        pairs, assigned = route_hops(conn, route, chain_id, body.get("ports")) if route else ([], [])
        group_id = check_group(conn, "chains", body.get("group_id", old["group_id"] if old else None))
        if old:
            conn.execute("UPDATE chains SET remark=?,route_id=?,target_id=? WHERE id=?",
                         (remark, route_id, target["id"], chain_id))
            if group_id != old["group_id"]:
                move_group(conn, "chains", chain_id, group_id)
        else:
            chain_id = conn.execute("""INSERT INTO chains(remark,route_id,target_id,group_id,sort)
                VALUES (?,?,?,?,(SELECT COALESCE(MAX(sort),0)+1 FROM chains))""",
                (remark, route_id, target["id"], group_id)).lastrowid
        apply_config_hops(conn, chain_id, pairs)
        return {"id": chain_id, "assigned": assigned}

    def api_targets(self, conn, body):
        targets = []
        for t in conn.execute(ordered("targets")):
            refs = [dict(c) for c in conn.execute("SELECT id,remark FROM chains WHERE target_id=? ORDER BY id", (t["id"],))]
            targets.append({**dict(t), "params": json.loads(t["params"]), "configs": refs,
                            "uri": target_config.connection(t, t["host"], t["port"], t["name"])["uri"]})
        return {"targets": targets, "groups": get_groups(conn, "targets")}

    @track_mutation
    def api_target_save(self, conn, body, target_id=None):
        old = get_target(conn, target_id) if target_id is not None else None
        if "uri" in body:
            try:
                parsed = target_config.import_uri(body["uri"])
            except ValueError as e:
                raise UserError(str(e), "uri") from None
            host = check_host(parsed["host"], "uri")
            kind, port, params = parsed["protocol"], parsed["port"], target_config.canonical(parsed["params"])
            if kind == "vless-reality":
                check_host(parsed["params"]["server_name"], "uri")
            fallback = old["name"] if old else parsed["name"]
        elif old and old["type"] != "endpoint":
            if any(k in body for k in ("host", "port", "type", "params")):
                raise UserError("SS／VLESS 的连接参数请通过分享链接修改", "uri")
            host, port, kind, params, fallback = old["host"], old["port"], old["type"], old["params"], old["name"]
        else:
            if body.get("type", "endpoint") != "endpoint" or "params" in body:
                raise UserError("SS／VLESS 请导入分享链接", "uri")
            host = check_host(body.get("host", old["host"] if old else None), "host")
            port = check_port(body.get("port", old["port"] if old else None), "port")
            kind, params, fallback = "endpoint", "{}", old["name"] if old else host
        name = check_remark(body.get("name") or fallback, "name", "名称")
        group_id = check_group(conn, "targets", body.get("group_id", old["group_id"] if old else None))
        if old:
            conn.execute("UPDATE targets SET name=?,host=?,port=?,type=?,params=? WHERE id=?",
                         (name, host, port, kind, params, target_id))
            if group_id != old["group_id"]:
                move_group(conn, "targets", target_id, group_id)
        else:
            target_id = conn.execute("""INSERT INTO targets(name,host,port,type,params,group_id,sort)
                VALUES (?,?,?,?,?,?,(SELECT COALESCE(MAX(sort),0)+1 FROM targets))""",
                (name, host, port, kind, params, group_id)).lastrowid
        return {"id": target_id}

    @track_mutation
    def api_target_del(self, conn, body, target_id):
        get_target(conn, target_id)
        if conn.execute("SELECT 1 FROM chains WHERE target_id=?", (target_id,)).fetchone():
            raise UserError("目标仍被配置引用，请先解除引用")
        conn.execute("DELETE FROM targets WHERE id=?", (target_id,))
        return {"deleted": True}

    def api_settings(self, conn, body):
        return {"public_url": setting(conn, "public_url", ""), "sync_path": setting(conn, "sync_path"),
                "panel_path": setting(conn, "panel_path"), "username": setting(conn, "admin_user"),
                "version": VERSION, "components": COMPONENTS}

    def api_settings_save(self, conn, body):
        url = str(body.get("public_url", setting(conn, "public_url", ""))).strip().rstrip("/")
        if url and not re.fullmatch(r"https?://(?:[A-Za-z0-9.-]+|\[[0-9a-fA-F:]+\])(?::[0-9]{1,5})?", url):
            raise UserError("格式应为 http(s)://域名或IP[:端口]，不带路径", "public_url")
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
    if sqlite3.sqlite_version_info < (3, 35, 0):
        raise UserError("需要 SQLite 3.35 或以上版本")
    conn = init_db()
    load_agent()
    Handler.sync_path = setting(conn, "sync_path")
    Handler.panel_path = setting(conn, "panel_path")
    host, _, port = args.listen.rpartition(":")
    server = LimitedHTTPServer((host or "0.0.0.0", int(port)), Handler)
    print(f"ictl-server {VERSION} 监听 {args.listen}", flush=True)
    print_info(conn, args.listen)
    sys.stdout.flush()
    conn.close()
    threading.Thread(target=cleanup_loop, daemon=True).start()
    server.serve_forever()


def cmd_info(args):
    conn = read_db()
    try:
        print_info(conn)
    finally:
        conn.close()


def cmd_health(args):
    conn = read_db()
    try:
        if conn.execute('PRAGMA user_version').fetchone()[0] != 300:
            raise UserError('数据库尚未就绪')
        path = setting(conn, 'panel_path') + '/'
    finally:
        conn.close()
    client = http.client.HTTPConnection('127.0.0.1', 8730, timeout=2)
    try:
        client.request('GET', path)
        response = client.getresponse()
        if response.status != 200 or b'<title>ictl</title>' not in response.read():
            raise UserError('HTTP 服务尚未就绪')
    finally:
        client.close()


def cmd_reset_admin(args):
    conn = db()
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
    s = sub.add_parser("health", help="只读数据库与 HTTP 就绪检查")
    s.set_defaults(func=cmd_health)
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
