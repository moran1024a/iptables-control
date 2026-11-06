import time
import logging
import database
import settings


def delete_node(conn, node_id):
    node = database.get_node(conn, node_id)
    table = database.node_table(node["kind"])
    conn.execute(
        "INSERT OR REPLACE INTO archived_nodes VALUES (?,?,?)", (node_id, node["name"], int(time.time()))
    )
    conn.execute("DELETE FROM retired_ports WHERE node_id = ?", (node_id,))
    conn.execute("DELETE FROM sync_ops WHERE node_id = ?", (node_id,))
    conn.execute(f"DELETE FROM {table} WHERE id = ?", (node_id,))
    conn.execute("DELETE FROM windows WHERE node_id = ?", (node_id,))
    conn.execute("DELETE FROM active_windows WHERE node_id = ?", (node_id,))
    conn.execute("DELETE FROM events WHERE node_id = ?", (node_id,))
    conn.execute("DELETE FROM chain_pending WHERE node_id = ?", (node_id,))
    for table in ("node_snapshots", "metric_windows", "metric_hours", "agent_switches", "wake_jobs"):
        conn.execute(f"DELETE FROM {table} WHERE node_id=?", (node_id,))
    finish_deleted_chains(conn)


def delete_chain(conn, chain_id):
    if conn.execute("SELECT 1 FROM sqlite_master WHERE name='links'").fetchone():
        conn.execute("DELETE FROM links WHERE chain_id=? AND managed=0", (chain_id,))
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
    conn.execute(
        "DELETE FROM chain_pending WHERE chain_id = ?\n                    AND node_id NOT IN (SELECT node_id FROM hops WHERE chain_id = ?)\n                    AND node_id IN (SELECT id FROM nodes WHERE first_seen IS NULL)",
        (chain_id, chain_id),
    )


def finish_deleted_chains(conn):
    """删除中的转发在所有相关节点确认后真正删除。"""
    for r in conn.execute(
        "SELECT id FROM chains WHERE deleting = 1\n               AND id NOT IN (SELECT chain_id FROM chain_pending)"
    ).fetchall():
        delete_chain(conn, r["id"])


def add_event(conn, node_id, text, now=None):
    conn.execute(
        "INSERT INTO events (node_id, ts, text) VALUES (?, ?, ?)", (node_id, now or int(time.time()), text)
    )


def cleanup(conn):
    now = int(time.time())
    with conn:
        conn.execute("DELETE FROM windows WHERE win < ?", ((now - settings.RETENTION) // settings.WINDOW,))
        conn.execute(
            "DELETE FROM active_windows WHERE win < ?",
            ((now - settings.RETENTION) // settings.ACTIVE_WINDOW,),
        )
        conn.execute("DELETE FROM events WHERE ts < ?", (now - settings.RETENTION,))
        conn.execute("DELETE FROM sessions WHERE expires < ?", (now,))
        conn.execute(
            "DELETE FROM sync_ops WHERE expires < ? AND (result IS NULL OR result != 'failed')",
            (now - settings.RETENTION,),
        )
        conn.execute("DELETE FROM deleted_receipts WHERE expires < ?", (now,))


def mark_config_nodes(conn, chain_id):
    mark_pending(
        conn,
        chain_id,
        [r[0] for r in conn.execute("SELECT node_id FROM hops WHERE chain_id = ?", (chain_id,))],
    )


def cleanup_loop():
    while True:
        conn = None
        try:
            conn = database.db()
            cleanup(conn)
        except Exception:
            logging.exception("database cleanup")
        finally:
            if conn is not None:
                conn.close()
        time.sleep(3600)
