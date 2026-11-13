import time
import logging
import database
import settings


def delete_node(conn, node_id):
    node = database.get_node(conn, node_id)
    table = database.node_table(node["kind"])
    conn.execute(
        "INSERT OR REPLACE INTO archived_nodes VALUES (?,?,?)",
        (node_id, node["name"], int(time.time())),
    )
    conn.execute("DELETE FROM retired_ports WHERE node_id = ?", (node_id,))
    # Only cleanup of links that are already being deleted can remain here.
    conn.execute("DELETE FROM retirements WHERE node_id = ?", (node_id,))
    conn.execute("DELETE FROM rules WHERE node_id = ?", (node_id,))
    conn.execute("DELETE FROM traffic_epochs WHERE node_id = ?", (node_id,))
    conn.execute("DELETE FROM sync_ops WHERE node_id = ?", (node_id,))
    conn.execute(f"DELETE FROM {table} WHERE id = ?", (node_id,))
    conn.execute("DELETE FROM windows WHERE node_id = ?", (node_id,))
    conn.execute("DELETE FROM active_windows WHERE node_id = ?", (node_id,))
    conn.execute("DELETE FROM events WHERE node_id = ?", (node_id,))
    for table in (
        "node_snapshots",
        "metric_windows",
        "metric_hours",
        "agent_switches",
        "wake_jobs",
    ):
        conn.execute(f"DELETE FROM {table} WHERE node_id=?", (node_id,))


def add_event(conn, node_id, text, now=None):
    conn.execute(
        "INSERT INTO events (node_id, ts, text) VALUES (?, ?, ?)",
        (node_id, now or int(time.time()), text),
    )


def cleanup(conn):
    now = int(time.time())
    with conn:
        conn.execute(
            "DELETE FROM windows WHERE win < ?", ((now - settings.RETENTION) // settings.WINDOW,)
        )
        conn.execute(
            "DELETE FROM active_windows WHERE win < ?",
            ((now - settings.RETENTION) // settings.ACTIVE_WINDOW,),
        )
        conn.execute("DELETE FROM events WHERE ts < ?", (now - settings.RETENTION,))
        conn.execute("DELETE FROM configuration_events WHERE ts < ?", (now - settings.RETENTION,))
        conn.execute("DELETE FROM sessions WHERE expires < ?", (now,))
        conn.execute(
            "DELETE FROM sync_ops WHERE expires < ? AND (result IS NULL OR result != 'failed')",
            (now - settings.RETENTION,),
        )
        conn.execute("DELETE FROM deleted_receipts WHERE expires < ?", (now,))


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
