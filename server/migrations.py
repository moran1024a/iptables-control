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
            conn.execute("PRAGMA user_version=602")
        version = 602
    if version == 602:
        database.backup_database(conn, str(path) + ".pre-0.6.0-rc.7.bak")
        with database.atomic(conn):
            upgrade_603(conn)
            if conn.execute("PRAGMA foreign_key_check").fetchall():
                raise errors.UserError("升级外键检查失败，数据库未修改")
            conn.execute("PRAGMA user_version=603")
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


def upgrade_603(conn):
    """Discard managed Reality once, keeping cleanup receipts outside deleted configs."""
    import node_sync

    before = node_sync.mutation_snapshot(conn)
    removed = {r[0] for r in conn.execute("SELECT id FROM targets WHERE type='vless-reality'")}

    def uses_reality(doc):
        if not doc:
            return False
        target = doc.get("target")
        if isinstance(target, dict) and target.get("type") == "vless-reality":
            return True
        plan = doc.get("plan", doc)
        return (isinstance(plan.get("target"), int) and plan["target"] in removed
                or any(v.get("target_id") in removed for v in plan.get("graph", {}).get("vertices", [])))

    empty_topologies = set()
    for row in conn.execute("SELECT id,graph FROM topologies").fetchall():
        graph = json.loads(row["graph"])
        drop = {v["id"] for v in graph["vertices"] if v.get("target_id") in removed}
        if not drop:
            continue
        for v in graph["vertices"]:
            if v["kind"] == "direct" and any(v["id"] in edge and any(x in drop for x in edge) for edge in graph["edges"]):
                drop.add(v["id"])
        graph["vertices"] = [v for v in graph["vertices"] if v["id"] not in drop]
        graph["edges"] = [e for e in graph["edges"] if not any(x in drop for x in e)]
        for key in ("bindings", "ports"):
            graph[key] = {k:v for k,v in graph.get(key, {}).items() if not any(x in drop for x in k.split("/"))}
        graph["address_book"] = {k:v for k,v in graph.get("address_book", {}).items() if k not in drop}
        if not any(v["kind"] == "target" for v in graph["vertices"]):
            empty_topologies.add(row["id"])
        conn.execute("UPDATE topologies SET graph=?,revision=revision+1 WHERE id=?", (json.dumps(graph), row["id"]))

    held = set()
    now = int(time.time())
    for row in conn.execute("SELECT * FROM configurations").fetchall():
        desired = json.loads(row["desired"]) if row["desired"] else None
        applied = json.loads(row["applied"]) if row["applied"] else None
        job_row = conn.execute("SELECT payload FROM jobs WHERE configuration_id=?", (row["id"],)).fetchone()
        job = json.loads(job_row[0]) if job_row else {}
        deleted = row["topology_id"] in empty_topologies or uses_reality(desired)
        if not (deleted or uses_reality(applied) or uses_reality(job)):
            continue
        rules = list(conn.execute("SELECT node_id,port FROM rules WHERE configuration_id=?", (row["id"],)))
        rules += list(conn.execute("SELECT node_id,port FROM retirements WHERE configuration_id=?", (row["id"],)))
        rules += (applied or {}).get("rules", []) + job.get("old", []) + job.get("new", [])
        held.update((r["node_id"], r["port"]) for r in rules)
        conn.execute("DELETE FROM jobs WHERE configuration_id=?", (row["id"],))
        conn.execute("DELETE FROM rules WHERE configuration_id=?", (row["id"],))
        conn.execute("DELETE FROM retirements WHERE configuration_id=?", (row["id"],))
        conn.execute("UPDATE probe_bindings SET ended=? WHERE scope='topologies' AND owner_id=? AND section LIKE ? AND ended IS NULL",
                     (now, row["topology_id"], f"config:{row['id']}:%"))
        if deleted or row["deleted"] or not desired:
            conn.execute("DELETE FROM configurations WHERE id=?", (row["id"],))
        else:
            conn.execute("UPDATE configurations SET applied=NULL,state='pending',error='',failover='{}',revision=revision+1 WHERE id=?", (row["id"],))
    for ident in empty_topologies:
        conn.execute("DELETE FROM topologies WHERE id=?", (ident,))
    for nid, port in held:
        if not conn.execute("SELECT 1 FROM rules WHERE node_id=? AND port=?", (nid,port)).fetchone():
            conn.execute("INSERT OR IGNORE INTO retired_ports VALUES (?,?)", (nid,port))
    conn.execute("DELETE FROM targets WHERE type='vless-reality'")
    # targets has no inbound SQL foreign keys; embedded references were cleaned above.
    ddl = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='targets'").fetchone()[0]
    conn.execute(ddl.replace("CREATE TABLE targets", "CREATE TABLE targets_603", 1).replace("'vless-reality'", "'vless-enc'"))
    conn.execute("INSERT INTO targets_603 SELECT * FROM targets")
    conn.execute("DROP TABLE targets")
    conn.execute("ALTER TABLE targets_603 RENAME TO targets")
    conn.execute("CREATE TABLE external_configurations (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, uri TEXT NOT NULL, group_id INTEGER NOT NULL REFERENCES item_groups(id), sort INTEGER NOT NULL DEFAULT 0, revision INTEGER NOT NULL DEFAULT 1)")
    conn.execute("CREATE TABLE subscription_external_configurations (subscription_id INTEGER NOT NULL REFERENCES subscriptions(id) ON DELETE CASCADE, external_configuration_id INTEGER NOT NULL REFERENCES external_configurations(id) ON DELETE CASCADE, PRIMARY KEY(subscription_id,external_configuration_id))")
    conn.execute("INSERT INTO item_groups(kind,name,builtin) VALUES ('external_configurations',?,1)", (settings.DEFAULT_GROUP,))
    node_sync.finish_mutation(conn, before)
