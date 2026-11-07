import json
import os
import re
import secrets
import sqlite3
import time
import agent_v3
import database
import errors
import forwarding
import lifecycle
import link_quality
import network_quality
import pools
import probe_store
import security
import settings
import subscriptions
import wake
import topology
import node_addresses

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


def init_legacy_db():
    d = os.path.dirname(settings.DB_PATH)
    if d:
        os.makedirs(d, mode=448, exist_ok=True)
    conn = database.db()
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version > 202:
        conn.close()
        raise errors.UserError("数据库版本高于本服务端，请使用兼容版本")
    if version == 202:
        return conn
    exists = conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'nodes'").fetchone()
    if exists:
        backup_path = settings.DB_PATH + (
            ".pre-0.2.0.bak"
            if version < 200
            else ".pre-layout-201.bak"
            if version == 200
            else ".pre-0.2.1.bak"
        )
        if not os.path.exists(backup_path):
            temporary = backup_path + ".tmp"
            backup = sqlite3.connect(temporary)
            try:
                conn.backup(backup)
            finally:
                backup.close()
            os.chmod(temporary, 384)
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
    cols = lambda t: [r["name"] for r in conn.execute(f"PRAGMA table_info({t})")]
    if "deleting" not in cols("chains"):
        conn.execute("ALTER TABLE chains ADD COLUMN deleting INTEGER NOT NULL DEFAULT 0")
    if "remark" in cols("nodes"):
        conn.execute("ALTER TABLE nodes RENAME COLUMN remark TO name")
    if "note" not in cols("nodes"):
        conn.execute("ALTER TABLE nodes ADD COLUMN note TEXT NOT NULL DEFAULT ''")
    for t in ("nodes", "chains"):
        if "sort" not in cols(t):
            conn.execute(f"ALTER TABLE {t} ADD COLUMN sort INTEGER NOT NULL DEFAULT 0")
            conn.execute(f"UPDATE {t} SET sort = id")
    if "ports" not in cols("nodes"):
        conn.execute("ALTER TABLE nodes ADD COLUMN ports TEXT NOT NULL DEFAULT '10000-65535'")
        conn.execute(
            "UPDATE nodes SET ports = (SELECT MIN(port) FROM hops WHERE hops.node_id = nodes.id) || '-65535'\n                        WHERE (SELECT MIN(port) FROM hops WHERE hops.node_id = nodes.id) < 10000"
        )
    for t in ("nodes", "chains"):
        if "group_id" not in cols(t):
            conn.execute(f"ALTER TABLE {t} ADD COLUMN group_id INTEGER NOT NULL DEFAULT 0")
        if not conn.execute("SELECT 1 FROM item_groups WHERE kind = ? AND builtin = 1", (t,)).fetchone():
            conn.execute(
                "INSERT INTO item_groups (kind, name, builtin, sort) VALUES (?, ?, 1, 0)",
                (t, settings.DEFAULT_GROUP),
            )
        conn.execute(
            f"UPDATE {t} SET group_id = ? WHERE group_id NOT IN (SELECT id FROM item_groups WHERE kind = ?)",
            (database.default_group(conn, t), t),
        )
    if "auto_update" not in cols("nodes"):
        conn.execute("ALTER TABLE nodes ADD COLUMN auto_update INTEGER")
    old_window = int(database.setting(conn, "window", 30))
    if old_window != settings.WINDOW:
        rows = conn.execute("SELECT node_id, win, level FROM windows").fetchall()
        conn.execute("DELETE FROM windows")
        for r in rows:
            conn.execute(
                "INSERT INTO windows VALUES (?, ?, ?)\n                            ON CONFLICT (node_id, win) DO UPDATE SET level = MIN(level, excluded.level)",
                (r["node_id"], r["win"] * old_window // settings.WINDOW, r["level"]),
            )
        database.set_setting(conn, "window", str(settings.WINDOW))
    conn.commit()
    with conn:
        if not database.setting(conn, "sync_path"):
            database.set_setting(conn, "sync_path", "/" + secrets.token_hex(12))
        if not database.setting(conn, "panel_path"):
            database.set_setting(conn, "panel_path", "/" + secrets.token_hex(8))
        if not database.setting(conn, "admin_user"):
            security.reset_admin(conn)
    migrate_v020(conn)
    migrate_layout(conn)
    migrate_imports(conn)
    return conn


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
    "reported_mode": "TEXT",
    "desired_generation": "INTEGER NOT NULL DEFAULT 1",
    "applied_generation": "INTEGER NOT NULL DEFAULT 0",
    "manual_required": "INTEGER NOT NULL DEFAULT 0",
    "protocol_version": "INTEGER NOT NULL DEFAULT 1",
    "last_manual": "INTEGER",
    "last_success": "INTEGER",
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
    for statement in SYNC_SCHEMA.split(";"):
        if statement.strip():
            conn.execute(statement)


def ensure_active_windows(conn):
    conn.execute(
        "CREATE TABLE IF NOT EXISTS active_windows (\n        node_id INTEGER NOT NULL, win INTEGER NOT NULL, level INTEGER NOT NULL,\n        PRIMARY KEY(node_id, win))"
    )
    if database.setting(conn, "active_window_start") is None:
        database.set_setting(conn, "active_window_start", str(int(time.time())))


def init_db_v030():
    os.makedirs(os.path.dirname(os.path.abspath(settings.DB_PATH)), mode=448, exist_ok=True)
    conn = database.db()
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version > 300:
        conn.close()
        raise errors.UserError("数据库版本高于本服务端，请使用兼容版本")
    if version == 300:
        with conn:
            conn.execute("BEGIN IMMEDIATE")
            ensure_active_windows(conn)
        return conn
    exists = conn.execute("SELECT 1 FROM sqlite_master WHERE name='nodes'").fetchone()
    if not exists:
        conn.execute("PRAGMA journal_mode=WAL")
        schema = SCHEMA.replace(
            SCHEMA[
                SCHEMA.index("CREATE TABLE IF NOT EXISTS chains") : SCHEMA.index(
                    "CREATE TABLE IF NOT EXISTS hops"
                )
            ],
            TARGET_SCHEMA + CHAIN_SCHEMA + ";\n",
        )
        conn.executescript("BEGIN IMMEDIATE;\n" + schema)
        with conn:
            conn.execute(
                "CREATE TABLE routes(id INTEGER PRIMARY KEY,name TEXT NOT NULL,node_ids TEXT NOT NULL,group_id INTEGER NOT NULL,sort INTEGER NOT NULL DEFAULT 0)"
            )
            conn.execute(
                "CREATE TABLE retired_ports(node_id INTEGER NOT NULL,port INTEGER NOT NULL,PRIMARY KEY(node_id,port))"
            )
            conn.execute(
                "ALTER TABLE nodes ADD COLUMN kind TEXT NOT NULL DEFAULT 'forward' CHECK(kind='forward')"
            )
            add_sync_schema(conn)
            for kind in settings.KINDS:
                conn.execute(
                    "INSERT INTO item_groups(kind,name,builtin) VALUES (?,?,1)",
                    (kind, settings.DEFAULT_GROUP),
                )
            for k, v in (
                ("sync_path", "/" + secrets.token_hex(12)),
                ("panel_path", "/" + secrets.token_hex(8)),
                ("window", str(settings.WINDOW)),
                ("next_node_id", "1"),
                ("next_hop_id", "1"),
            ):
                database.set_setting(conn, k, v)
            security.reset_admin(conn)
            conn.execute("PRAGMA user_version=300")
        return conn
    database.backup_database(conn, settings.DB_PATH + ".pre-0.3.0.bak")
    if version < 202:
        conn.close()
        conn = init_legacy_db()
    try:
        migrate_v030(conn)
    except Exception:
        conn.close()
        raise
    return conn


def init_db():
    version = None
    conn = database.db() if os.path.isfile(settings.DB_PATH) else None
    if conn is not None:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if version > settings.DATABASE_VERSION:
            conn.close()
            raise errors.UserError("数据库版本高于本服务端")
        if version >= 400:
            network_quality.migrate(conn)
            wake.migrate(conn)
            pools.migrate(conn)
            migrate_v044(conn)
            migrate_v047(conn)
            migrate_v047_rc2(conn)
            topology.migrate(conn)
            with conn:
                node_addresses.migrate(conn)
            topology.migrate_nodes(conn)
            migrate_config_names(conn)
            node_addresses.migrate_primary(conn)
            with conn:
                ensure_active_windows(conn)
            return conn
        conn.close()
    conn = init_db_v030()
    agent_v3.migrate(conn, reset_legacy=version is not None and version < 400)
    network_quality.migrate(conn)
    wake.migrate(conn)
    pools.migrate(conn)
    migrate_v044(conn)
    migrate_v047(conn)
    migrate_v047_rc2(conn)
    topology.migrate(conn)
    with conn:
        node_addresses.migrate(conn)
    topology.migrate_nodes(conn)
    migrate_config_names(conn)
    node_addresses.migrate_primary(conn)
    return conn


def migrate_config_names(conn):
    if conn.execute('PRAGMA user_version').fetchone()[0] >= 503:
        return
    database.backup_database(conn, settings.DB_PATH + '.pre-config-names.bak')
    with conn:
        conn.execute('BEGIN IMMEDIATE')
        if 'config_name' not in {r['name'] for r in conn.execute('PRAGMA table_info(links)')}:
            conn.execute("ALTER TABLE links ADD COLUMN config_name TEXT NOT NULL DEFAULT ''")
        conn.execute("""UPDATE links SET config_name=CASE
            WHEN managed=0 AND chain_id IS NOT NULL THEN name
            ELSE printf('配置 %04d',id) END WHERE config_name=''""")
        conn.execute('PRAGMA user_version=503')


def migrate_v047_rc2(conn):
    if conn.execute("PRAGMA user_version").fetchone()[0] >= 406:
        return
    database.backup_database(conn, settings.DB_PATH + ".pre-0.4.7-rc.2.bak")
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        for table, fields in {
            "links": {"applied": "TEXT", "failure_scope": "TEXT NOT NULL DEFAULT ''"},
            "probe_samples": {"epoch": "TEXT", "previous_slot": "INTEGER"},
        }.items():
            columns = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
            for name, ddl in fields.items():
                if name not in columns:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")
        for row in conn.execute("SELECT * FROM links").fetchall():
            link = dict(row)
            if link["applied"]:
                continue
            plan = json.loads(link["plan"])
            selected = json.loads(link["selected"])
            previous = None
            if link["job"]:
                previous = json.loads(link["job"]).get("previous")
                if previous:
                    plan, selected = previous["plan"], previous["selected"]
            hops = list(conn.execute("SELECT * FROM hops WHERE chain_id=? ORDER BY seq", (link["chain_id"],)))
            expected = ([plan["entry"]] if plan["entry"] else []) + [
                n for st in plan["stages"]
                if (n := st["node_id"] if st["kind"] == "node" else selected.get(st["id"]))
            ]
            chain = conn.execute("SELECT * FROM chains WHERE id=?", (link["chain_id"],)).fetchone()
            trustworthy = previous is not None or link["state"] not in ("error", "waiting", "switching", "preparing")
            if trustworthy and chain and expected == [h["node_id"] for h in hops] and plan["target"] == chain["target_id"]:
                if hops:
                    plan["port"] = hops[0]["port"]
                target = dict(database.get_target(conn, plan["target"]))
                old_rules = json.loads(link["job"]).get("old", []) if link["job"] else pools.rows(conn, link["id"])
                last = next((r for r in old_rules if hops and r["id"] == hops[-1]["id"]), None)
                if last:
                    target.update(host=last["host"], port=last["tport"])
                pools.save_applied(conn, link["id"], plan, target)
        database.set_setting(conn, "probe_identity_v2", str(int(time.time())))
        conn.execute("PRAGMA user_version=406")


def migrate_v047(conn):
    if conn.execute("PRAGMA user_version").fetchone()[0] >= 405:
        return
    database.backup_database(conn, settings.DB_PATH + ".pre-0.4.7.bak")
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        probe_store.install_schema(conn)
        database.set_setting(conn, "probe_cutover", str(int(time.time())))
        conn.execute("PRAGMA user_version=405")


def migrate_v044(conn):
    if conn.execute("PRAGMA user_version").fetchone()[0] >= 404:
        return
    database.backup_database(conn, settings.DB_PATH + ".pre-0.4.4.bak")
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        if not conn.execute("SELECT 1 FROM item_groups WHERE kind='links' AND builtin=1").fetchone():
            conn.execute(
                "INSERT INTO item_groups(kind,name,builtin) VALUES ('links',?,1)", (settings.DEFAULT_GROUP,)
            )
        conn.execute("ALTER TABLE links ADD COLUMN group_id INTEGER NOT NULL DEFAULT 0")
        conn.execute("ALTER TABLE links ADD COLUMN sort INTEGER NOT NULL DEFAULT 0")
        conn.execute("UPDATE links SET group_id=?,sort=id", (database.default_group(conn, "links"),))
        for stmt in (subscriptions.SCHEMA + link_quality.SCHEMA).split(";"):
            if stmt.strip():
                conn.execute(stmt)
        conn.execute("PRAGMA user_version=404")


def migrate_v030(conn):
    """Delete only managed tunnel data; preserve external credentials and forwarding IDs."""
    managed = {r[0] for r in conn.execute("SELECT id FROM tunnels WHERE source='agent'")}
    removed = {
        r[0]
        for r in conn.execute(
            "SELECT id FROM chains WHERE tunnel_id IN (SELECT id FROM tunnels WHERE source='agent')"
        )
    }
    old_nodes = conn.execute("SELECT * FROM nodes").fetchall()
    before = {n["id"]: forwarding.legacy_node_rules(conn, n) for n in old_nodes}
    expected = {
        n["id"]: [
            f"{r['id']} both {r['port']} {r['host']} {r['tport']}"
            for r in forwarding.legacy_node_rule_rows(conn, n)
            if r["chain_id"] not in removed
        ]
        for n in old_nodes
    }
    tunnel_count = conn.execute("SELECT COUNT(*) FROM tunnel_nodes").fetchone()[0]
    conn.execute("PRAGMA foreign_keys=OFF")
    try:
        with conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(TARGET_SCHEMA)
            conn.execute("UPDATE item_groups SET kind='targets' WHERE kind='tunnels'")
            conn.execute(
                "INSERT INTO targets(id,name,type,host,port,params,group_id,sort)\n                SELECT id,name,protocol,external_host,port,params,group_id,sort FROM tunnels WHERE source='external'"
            )
            external_count = conn.execute("SELECT COUNT(*) FROM targets").fetchone()[0]
            for cid in removed:
                for h in conn.execute("SELECT * FROM hops WHERE chain_id=?", (cid,)).fetchall():
                    forwarding.reserve_removed(conn, h["node_id"], h["port"])
                lifecycle.delete_chain(conn, cid)
            conn.execute(CHAIN_SCHEMA.replace("CREATE TABLE chains", "CREATE TABLE chains_new"))
            for c in conn.execute("SELECT * FROM chains ORDER BY id").fetchall():
                tid = c["tunnel_id"]
                if tid is None:
                    tid = conn.execute(
                        "INSERT INTO targets(name,type,host,port,group_id,sort) VALUES (?,'endpoint',?,?,?,?)",
                        (
                            c["remark"],
                            c["target_host"],
                            c["target_port"],
                            database.default_group(conn, "targets"),
                            c["sort"],
                        ),
                    ).lastrowid
                conn.execute(
                    "INSERT INTO chains_new(id,remark,target_id,route_id,deleting,sort,group_id) VALUES (?,?,?,?,?,?,?)",
                    (c["id"], c["remark"], tid, c["route_id"], c["deleting"], c["sort"], c["group_id"]),
                )
            conn.execute("DROP TABLE chains")
            conn.execute("ALTER TABLE chains_new RENAME TO chains")
            conn.execute("DROP VIEW all_nodes")
            for table in ("windows", "events", "chain_pending", "retired_ports"):
                conn.execute(f"DELETE FROM {table} WHERE node_id IN (SELECT id FROM tunnel_nodes)")
            for table in ("tunnels", "tunnel_presence", "tunnel_nodes"):
                conn.execute(f"DROP TABLE {table}")
            conn.execute("DELETE FROM item_groups WHERE kind='tunnel_nodes'")
            conn.execute("DELETE FROM settings WHERE k IN ('tunnel_path','tunnel_heartbeat')")
            for column in ("core_version", "online_until"):
                conn.execute(f"ALTER TABLE nodes DROP COLUMN {column}")
            add_sync_schema(conn)
            for n in old_nodes:
                lines = forwarding.node_rules(conn, n)
                if lines != expected[n["id"]]:
                    raise errors.UserError("升级校验失败：保留配置的规则发生变化")
                applied = int(
                    before[n["id"]] == lines
                    and n["state"] == "ok"
                    and (n["reported_v"] == forwarding.rules_version(lines))
                )
                conn.execute("UPDATE nodes SET applied_generation=? WHERE id=?", (applied, n["id"]))
            if conn.execute("PRAGMA foreign_key_check").fetchall():
                raise errors.UserError("升级校验失败：引用不完整")
            conn.execute("PRAGMA user_version=300")
    finally:
        conn.execute("PRAGMA foreign_keys=ON")
    print(
        f"0.3.0 迁移完成：删除隧道节点 {tunnel_count}、受管隧道 {len(managed)}、关联配置 {len(removed)}；保留外部目标 {external_count}。备份：{settings.DB_PATH}.pre-0.3.0.bak；远端隧道服务请自行卸载。",
        flush=True,
    )


def migrate_v020(conn):
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version > 200:
        raise errors.UserError("数据库版本高于本服务端，请使用兼容版本")
    if version == 200:
        return
    before = {n["id"]: forwarding.legacy_node_rules(conn, n) for n in conn.execute("SELECT * FROM nodes")}
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("ALTER TABLE nodes ADD COLUMN kind TEXT NOT NULL DEFAULT 'forward'")
        conn.execute("ALTER TABLE nodes ADD COLUMN core_version TEXT")
        conn.execute("ALTER TABLE nodes ADD COLUMN online_until INTEGER NOT NULL DEFAULT 0")
        conn.execute(
            "CREATE TABLE routes (id INTEGER PRIMARY KEY, name TEXT NOT NULL, node_ids TEXT NOT NULL)"
        )
        conn.execute(
            "CREATE TABLE tunnels (\n            id INTEGER PRIMARY KEY, name TEXT NOT NULL, node_id INTEGER NOT NULL REFERENCES nodes(id),\n            protocol TEXT NOT NULL, port INTEGER NOT NULL, params TEXT NOT NULL,\n            deleting INTEGER NOT NULL DEFAULT 0, UNIQUE(node_id, port))"
        )
        conn.execute("ALTER TABLE chains ADD COLUMN route_id INTEGER REFERENCES routes(id)")
        conn.execute("ALTER TABLE chains ADD COLUMN tunnel_id INTEGER REFERENCES tunnels(id)")
        conn.execute("ALTER TABLE chains ADD COLUMN ready INTEGER NOT NULL DEFAULT 1")
        conn.execute(
            "CREATE TABLE retired_ports (\n            node_id INTEGER NOT NULL REFERENCES nodes(id), port INTEGER NOT NULL,\n            PRIMARY KEY(node_id, port))"
        )
        conn.execute(
            "CREATE TABLE tunnel_presence (\n            node_id INTEGER NOT NULL REFERENCES nodes(id), start INTEGER NOT NULL, end INTEGER NOT NULL,\n            PRIMARY KEY(node_id, start))"
        )
        for c in conn.execute("SELECT * FROM chains ORDER BY id").fetchall():
            ids = [
                h[0]
                for h in conn.execute("SELECT node_id FROM hops WHERE chain_id = ? ORDER BY seq", (c["id"],))
            ]
            rid = conn.execute(
                "INSERT INTO routes(name, node_ids) VALUES (?, ?)", (c["remark"], json.dumps(ids))
            ).lastrowid
            conn.execute("UPDATE chains SET route_id = ? WHERE id = ?", (rid, c["id"]))
        database.set_setting(
            conn, "next_hop_id", str(conn.execute("SELECT COALESCE(MAX(id), 0) + 1 FROM hops").fetchone()[0])
        )
        database.set_setting(conn, "tunnel_path", "/" + secrets.token_hex(12))
        database.set_setting(conn, "tunnel_heartbeat", "10")
        after = {n["id"]: forwarding.legacy_node_rules(conn, n) for n in conn.execute("SELECT * FROM nodes")}
        if before != after:
            raise errors.UserError("升级校验失败：转发规则发生变化，迁移已回滚")
        conn.execute("PRAGMA user_version = 200")


def migrate_layout(conn):
    """拆分节点存储与五类分组；不重编号节点、凭据、转发规则。"""
    original = [dict(n) for n in conn.execute("SELECT * FROM nodes ORDER BY id")]
    before = {n["id"]: forwarding.legacy_node_rules(conn, n) for n in original if n["kind"] == "forward"}
    conn.execute("PRAGMA foreign_keys = OFF")
    try:
        with conn:
            conn.execute("BEGIN IMMEDIATE")
            schema = conn.execute("SELECT sql FROM sqlite_master WHERE name = 'nodes'").fetchone()[0]
            schema = re.sub('CREATE TABLE (?:"nodes"|nodes)', "CREATE TABLE tunnel_nodes", schema, count=1)
            schema = schema.replace("DEFAULT 'forward'", "DEFAULT 'tunnel'")
            conn.execute(schema)
            conn.execute("INSERT INTO tunnel_nodes SELECT * FROM nodes WHERE kind = 'tunnel'")
            for g in conn.execute(
                "SELECT * FROM item_groups WHERE kind = 'nodes'\n                    AND (builtin = 1 OR id IN (SELECT group_id FROM tunnel_nodes)) ORDER BY id"
            ).fetchall():
                gid = conn.execute(
                    "INSERT INTO item_groups(kind,name,builtin,sort) VALUES ('tunnel_nodes',?,?,?)",
                    (g["name"], g["builtin"], g["sort"]),
                ).lastrowid
                conn.execute("UPDATE tunnel_nodes SET group_id = ? WHERE group_id = ?", (gid, g["id"]))
            for table, reference in (
                ("tunnels", "REFERENCES tunnel_nodes(id)"),
                ("tunnel_presence", "REFERENCES tunnel_nodes(id)"),
                ("retired_ports", ""),
            ):
                ddl = conn.execute("SELECT sql FROM sqlite_master WHERE name = ?", (table,)).fetchone()[0]
                ddl = re.sub(
                    'CREATE TABLE (?:"' + table + '"|' + table + ")",
                    "CREATE TABLE " + table + "_new",
                    ddl,
                    count=1,
                )
                ddl = re.sub("REFERENCES\\s+nodes\\s*\\(id\\)", reference, ddl)
                conn.execute(ddl)
                conn.execute(f"INSERT INTO {table}_new SELECT * FROM {table}")
                conn.execute(f"DROP TABLE {table}")
                conn.execute(f"ALTER TABLE {table}_new RENAME TO {table}")
            conn.execute("DELETE FROM nodes WHERE kind = 'tunnel'")
            conn.execute("CREATE VIEW all_nodes AS SELECT * FROM nodes UNION ALL SELECT * FROM tunnel_nodes")
            database.set_setting(
                conn,
                "next_node_id",
                str(conn.execute("SELECT COALESCE(MAX(id), 0) + 1 FROM all_nodes").fetchone()[0]),
            )
            for table in ("routes", "tunnels"):
                gid = conn.execute(
                    "INSERT INTO item_groups(kind,name,builtin,sort) VALUES (?,?,1,0)",
                    (table, settings.DEFAULT_GROUP),
                ).lastrowid
                conn.execute(f"ALTER TABLE {table} ADD COLUMN group_id INTEGER NOT NULL DEFAULT 0")
                conn.execute(f"ALTER TABLE {table} ADD COLUMN sort INTEGER NOT NULL DEFAULT 0")
                conn.execute(f"UPDATE {table} SET group_id = ?, sort = id", (gid,))
            if conn.execute("PRAGMA foreign_key_check").fetchall():
                raise errors.UserError("节点隔离迁移失败：引用完整性校验未通过")
            migrated = [dict(n) for n in conn.execute("SELECT * FROM all_nodes ORDER BY id")]
            for rows in (original, migrated):
                for n in rows:
                    n.pop("group_id")
            if original != migrated:
                raise errors.UserError("节点隔离迁移失败：节点记录不一致")
            after = {
                n["id"]: forwarding.legacy_node_rules(conn, n) for n in conn.execute("SELECT * FROM nodes")
            }
            if before != after:
                raise errors.UserError("节点隔离迁移失败：转发规则发生变化")
            conn.execute("PRAGMA user_version = 201")
    finally:
        conn.execute("PRAGMA foreign_keys = ON")


def migrate_imports(conn):
    """允许外部隧道不关联 Agent，保留已有隧道 ID、端口、凭据与引用。"""
    conn.execute("PRAGMA foreign_keys = OFF")
    try:
        with conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "CREATE TABLE tunnels_new (\n                id INTEGER PRIMARY KEY, name TEXT NOT NULL,\n                node_id INTEGER REFERENCES tunnel_nodes(id),\n                protocol TEXT NOT NULL, port INTEGER NOT NULL, params TEXT NOT NULL,\n                deleting INTEGER NOT NULL DEFAULT 0, group_id INTEGER NOT NULL DEFAULT 0,\n                sort INTEGER NOT NULL DEFAULT 0, source TEXT NOT NULL DEFAULT 'agent',\n                external_host TEXT,\n                CHECK ((source = 'agent' AND node_id IS NOT NULL AND external_host IS NULL)\n                    OR (source = 'external' AND node_id IS NULL AND external_host IS NOT NULL)),\n                UNIQUE(node_id, port))"
            )
            conn.execute(
                "INSERT INTO tunnels_new(id,name,node_id,protocol,port,params,deleting,group_id,sort)\n                SELECT id,name,node_id,protocol,port,params,deleting,group_id,sort FROM tunnels"
            )
            conn.execute("DROP TABLE tunnels")
            conn.execute("ALTER TABLE tunnels_new RENAME TO tunnels")
            if conn.execute("PRAGMA foreign_key_check").fetchall():
                raise errors.UserError("隧道导入升级失败：引用完整性校验未通过")
            conn.execute("PRAGMA user_version = 202")
    finally:
        conn.execute("PRAGMA foreign_keys = ON")
