import os
import sqlite3
from contextlib import contextmanager
import errors
import settings


@contextmanager
def atomic(conn):
    """Join a caller's transaction without committing it prematurely."""
    if conn.in_transaction:
        conn.execute("SAVEPOINT use_case")
        try:
            yield conn
        except BaseException:
            conn.execute("ROLLBACK TO use_case")
            raise
        finally:
            conn.execute("RELEASE use_case")
    else:
        with conn:
            conn.execute("BEGIN IMMEDIATE")
            yield conn


def db():
    conn = sqlite3.connect(settings.DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def backup_database(conn, path):
    if os.path.exists(path):
        return
    temporary = path + ".tmp"
    backup = sqlite3.connect(temporary)
    try:
        conn.backup(backup)
    finally:
        backup.close()
    os.chmod(temporary, 384)
    os.replace(temporary, path)


def read_db():
    conn = sqlite3.connect("file:" + os.path.abspath(settings.DB_PATH) + "?mode=ro", uri=True, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def setting(conn, k, default=None):
    row = conn.execute("SELECT v FROM settings WHERE k = ?", (k,)).fetchone()
    return row["v"] if row else default


def set_setting(conn, k, v):
    conn.execute("INSERT OR REPLACE INTO settings VALUES (?, ?)", (k, v))


def default_group(conn, kind):
    return conn.execute("SELECT id FROM item_groups WHERE kind = ? AND builtin = 1", (kind,)).fetchone()[0]


def get_groups(conn, kind):
    return [
        dict(r)
        for r in conn.execute(
            "SELECT id, name, builtin FROM item_groups WHERE kind = ? ORDER BY sort, id", (kind,)
        )
    ]


def config_name(conn, name, group_id):
    group = conn.execute("SELECT name,builtin FROM item_groups WHERE id=?", (group_id,)).fetchone()
    return f"{group['name']}#{name}" if group and (not group["builtin"]) else name


def check_group(conn, kind, group_id):
    """校验所选分组；未选择时为默认分组。"""
    if group_id in (None, ""):
        return default_group(conn, kind)
    if (
        isinstance(group_id, int)
        and conn.execute("SELECT 1 FROM item_groups WHERE id = ? AND kind = ?", (group_id, kind)).fetchone()
    ):
        return group_id
    raise errors.UserError("分组不存在，请刷新后重试", "group_id")


def ordered(kind, where=""):
    """按分组顺序、再按组内顺序查询节点或转发。"""
    return f"SELECT t.* FROM {kind} t JOIN item_groups g ON g.id = t.group_id\n               {where} ORDER BY g.sort, g.id, t.sort, t.id"


def get_node(conn, node_id):
    if type(node_id) is not int or node_id <= 0:
        raise errors.UserError("无效的对象 ID", "node_id")
    node = conn.execute("SELECT * FROM nodes WHERE id = ?", (node_id,)).fetchone()
    if not node:
        raise errors.UserError("节点不存在", status=404)
    return node


def get_chain(conn, chain_id):
    if type(chain_id) is not int or chain_id <= 0:
        raise errors.UserError("无效的对象 ID", "chain_id")
    chain = conn.execute("SELECT * FROM chains WHERE id = ?", (chain_id,)).fetchone()
    if not chain:
        raise errors.UserError("转发不存在", status=404)
    return chain


def node_table(kind):
    if kind != "forward":
        raise errors.UserError("只支持 iptables 转发节点", "kind")
    return "nodes"


def move_group(conn, table, item_id, group_id):
    """换组后排在新组末尾，不改变业务配置或下发版本。"""
    conn.execute(
        f"UPDATE {table} SET group_id = ?, sort =\n                 (SELECT COALESCE(MAX(sort), 0) + 1 FROM {table} WHERE group_id = ?)\n                 WHERE id = ?",
        (group_id, group_id, item_id),
    )


def get_route(conn, route_id):
    if type(route_id) is not int or route_id <= 0:
        raise errors.UserError("无效的对象 ID", "route_id")
    r = conn.execute("SELECT * FROM routes WHERE id = ?", (route_id,)).fetchone()
    if not r:
        raise errors.UserError("链路不存在", "route_id", 404)
    return r


def get_target(conn, target_id):
    if type(target_id) is not int or target_id <= 0:
        raise errors.UserError("请选择目标", "target_id")
    target = conn.execute("SELECT * FROM targets WHERE id = ?", (target_id,)).fetchone()
    if not target:
        raise errors.UserError("目标不存在", "target_id", 404)
    return target
