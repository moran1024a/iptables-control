"""Current schema, RC upgrades, and the node-only import from 0.5.3."""

import os
import json
import secrets
import sqlite3
import time
from pathlib import Path
import database
import errors
import security
import settings


def create(conn):
    conn.executescript(Path(__file__).with_name("schema.sql").read_text())
    with conn:
        for kind in settings.KINDS:
            conn.execute(
                "INSERT INTO item_groups(kind,name,builtin) VALUES (?,?,1)",
                (kind, settings.DEFAULT_GROUP),
            )
        for key, value in dict(
            sync_path="/" + secrets.token_hex(12),
            panel_path="/" + secrets.token_hex(8),
            next_node_id="1",
            next_hop_id="1",
            active_window_start=str(int(time.time())),
        ).items():
            database.set_setting(conn, key, value)
        security.reset_admin(conn)
        conn.execute(f"PRAGMA user_version={settings.DATABASE_VERSION}")


def import_nodes(source, dest):
    """Preserve identity, not old business models. Receipts are transient handover data."""
    with dest:
        dest.execute("DELETE FROM item_groups WHERE kind='nodes'")
        group_ids = {}
        for group in source.execute(
            "SELECT * FROM item_groups WHERE kind='nodes' ORDER BY sort,id"
        ):
            group_ids[group["id"]] = dest.execute(
                "INSERT INTO item_groups(kind,name,builtin,sort) VALUES (?,?,?,?)",
                ("nodes", group["name"], group["builtin"], group["sort"]),
            ).lastrowid
        for table in ("nodes", "node_addresses", "agent_switches", "deleted_receipts"):
            fields = [r[1] for r in dest.execute(f"PRAGMA table_info({table})")]
            available = {r[1] for r in source.execute(f"PRAGMA table_info({table})")}
            fields = [f for f in fields if f in available]
            for row in source.execute(f"SELECT * FROM {table}"):
                values = dict(row)
                if table == "nodes":
                    values["group_id"] = group_ids[row["group_id"]]
                    values["desired_generation"] = (
                        max(row["desired_generation"], row["applied_generation"]) + 1
                    )
                    values.update(
                        connection_state="disconnected", state="init", err="", health=None
                    )
                dest.execute(
                    f'INSERT INTO {table}({",".join(fields)}) VALUES ({",".join("?" for _ in fields)})',
                    [values[f] for f in fields],
                )
        dest.execute("INSERT INTO migration_nodes SELECT id FROM nodes")
        for row in source.execute("SELECT k,v FROM settings"):
            if row["k"] in (
                "panel_path",
                "sync_path",
                "admin_user",
                "admin_hash",
                "must_change",
                "public_url",
                "next_node_id",
                "next_hop_id",
            ):
                database.set_setting(dest, row["k"], row["v"])
        held = set()
        for table in ("hops", "link_rules", "retired_ports", "link_retired"):
            held.update(tuple(r) for r in source.execute(f"SELECT node_id,port FROM {table}"))
        for row in source.execute("SELECT job FROM links WHERE job IS NOT NULL"):
            held.update((r["node_id"], r["port"]) for r in json.loads(row[0]).get("new", []))
        valid = {r[0] for r in dest.execute("SELECT id FROM nodes")}
        dest.executemany(
            "INSERT OR IGNORE INTO retired_ports VALUES (?,?)", [p for p in held if p[0] in valid]
        )
        database.set_setting(dest, "migration_from", "504")


def init_db():
    path = Path(settings.DB_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        conn = database.db()
        create(conn)
        os.chmod(path, 0o600)
        return conn
    conn = database.db()
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version == settings.DATABASE_VERSION:
        return conn
    if version == 600:
        database.backup_database(conn, str(path) + ".pre-0.6.0-rc.2.bak")
        with conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("ALTER TABLE nodes ADD COLUMN weight INTEGER NOT NULL DEFAULT 0")
            conn.execute("ALTER TABLE nodes ADD COLUMN runtime_state TEXT NOT NULL DEFAULT 'unknown'")
            conn.execute("ALTER TABLE nodes ADD COLUMN runtime_error TEXT NOT NULL DEFAULT ''")
            conn.execute("ALTER TABLE nodes ADD COLUMN runtime_at INTEGER NOT NULL DEFAULT 0")
            conn.execute("ALTER TABLE topologies ADD COLUMN failover_enabled INTEGER NOT NULL DEFAULT 0")
            conn.execute("ALTER TABLE configurations ADD COLUMN failover TEXT NOT NULL DEFAULT '{}'")
            conn.execute("PRAGMA user_version=601")
        version = 601
    if version == 601:
        database.backup_database(conn, str(path) + ".pre-0.6.0-rc.3.bak")
        with conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("ALTER TABLE targets ADD COLUMN addresses TEXT NOT NULL DEFAULT '[]'")
            conn.execute("CREATE TABLE tags (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE COLLATE NOCASE, color TEXT NOT NULL)")
            conn.execute("CREATE TABLE node_tags (node_id INTEGER NOT NULL REFERENCES nodes(id) ON DELETE CASCADE, tag_id INTEGER NOT NULL REFERENCES tags(id) ON DELETE CASCADE, PRIMARY KEY(node_id,tag_id))")
            conn.execute(f"PRAGMA user_version={settings.DATABASE_VERSION}")
        return conn
    if version != 504:
        conn.close()
        raise errors.UserError("此版本支持新安装、0.6.0 RC 升级或从 0.5.3 导入节点；更早版本请先更新到 0.5.3")
    database.backup_database(conn, str(path) + ".pre-0.6.0.bak")
    temporary = str(path) + ".importing"
    if os.path.exists(temporary):
        os.unlink(temporary)
    dest = sqlite3.connect(temporary)
    dest.row_factory = sqlite3.Row
    try:
        create(dest)
        import_nodes(conn, dest)
        if dest.execute("PRAGMA foreign_key_check").fetchall():
            raise errors.UserError("节点导入校验失败，旧数据库未改动")
        dest.close()
        conn.close()
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
        for suffix in ("-wal", "-shm"):
            if os.path.exists(str(path) + suffix):
                os.unlink(str(path) + suffix)
    except BaseException:
        dest.close()
        conn.close()
        if os.path.exists(temporary):
            os.unlink(temporary)
        raise
    return database.db()
