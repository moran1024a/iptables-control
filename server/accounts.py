"""Subaccounts and their use of shared forwarding nodes."""
import json
import re
import secrets
import time
import database
import errors
import node_sync
import quotas
import security
import settings


# Also gate reads between the expiry instant and the next cleanup cycle.
ACTIVE_SQL = "a.enabled=1 AND a.expiring=0 AND (a.expires_at IS NULL OR a.expires_at>unixepoch())"


def username(value):
    if type(value) is not str or not re.fullmatch("[A-Za-z0-9_.@-]{1,32}", value.strip()):
        raise errors.UserError("用户名为 1-32 位字母、数字或 _ . @ -", "username")
    return value.strip()


def password(value):
    if type(value) is not str or not 8 <= len(value) <= 128 or value == settings.DEFAULT_PASS:
        raise errors.UserError("请输入 8-128 位密码，且不能使用默认密码", "password")
    return value


def child(c, ident):
    row = c.execute("SELECT * FROM accounts WHERE id=?", (ident,)).fetchone()
    if row is None:
        raise errors.UserError("子账户不存在", status=404)
    return row


def topologies(c, account_id):
    return database.owned_rows(c, "topologies", account_id)


def listing(c):
    result = []
    for row in c.execute("SELECT id,username,enabled,revision,max_configs,expires_at,expiring FROM accounts ORDER BY id"):
        ident = row["id"]
        grants = database.allowed_nodes(c, ident)
        cleanup = c.execute(
            "SELECT COUNT(DISTINCT r.node_id) FROM retirements r "
            "JOIN configurations f ON f.id=r.configuration_id "
            "JOIN topologies t ON t.id=f.topology_id "
            "JOIN item_groups g ON g.id=t.group_id WHERE g.account_id=?",
            (ident,),
        ).fetchone()[0]
        expired = row["expires_at"] is not None and row["expires_at"] <= int(time.time())
        result.append(dict(row, enabled=bool(row["enabled"]), expiring=bool(row["expiring"] or expired),
                           node_ids=sorted(grants), cleanup_nodes=cleanup,
                           hidden_node_ids=sorted(database.hidden_nodes(c, ident)),
                           config_count=quotas.config_count(c, ident),
                           traffic=quotas.node_view(c, ident)))
    nodes = [dict(id=n["id"], name=n["name"], mode=n["desired_mode"], addr=n["addr"])
             for n in c.execute(database.ordered("nodes", "WHERE t.deleting=0"))]
    return dict(accounts=result, nodes=nodes)


def check_revision(row, body):
    if type(body.get("revision")) is not int or body["revision"] != row["revision"]:
        raise errors.UserError("账户已变更，请刷新后重试", status=409)


def save(c, body, ident=None):
    old = child(c, ident) if ident is not None else None
    now = int(time.time())
    if old and (old["expiring"] or old["expires_at"] is not None and old["expires_at"] <= now):
        raise errors.UserError("账户正在清理", status=409)
    if old:
        check_revision(old, body)
    name = username(body.get("username", old["username"] if old else None))
    reserved = (settings.DEFAULT_USER, database.setting(c, "admin_user"))
    if name.lower() in {x.lower() for x in reserved} or c.execute(
        "SELECT 1 FROM accounts WHERE username=? AND id IS NOT ?", (name, ident),
    ).fetchone():
        raise errors.UserError("用户名已使用", "username")
    enabled = body.get("enabled", bool(old["enabled"]) if old else True)
    if type(enabled) is not bool:
        raise errors.UserError("启用状态无效", "enabled")
    previous = database.allowed_nodes(c, ident) if old else set()
    values = body.get("node_ids", sorted(previous))
    if (not isinstance(values, list) or any(type(n) is not int or not 0 < n < 2**63 for n in values)
            or len(values) != len(set(values))):
        raise errors.UserError("请重新选择授权节点", "node_ids")
    grant = set(values)
    available = {row[0] for row in c.execute("SELECT id FROM nodes WHERE deleting=0")}
    if grant - previous - available:
        raise errors.UserError("部分节点不可用，请刷新后重试", "node_ids")
    previous_hidden = database.hidden_nodes(c, ident) if old else set()
    hidden = body.get("hidden_node_ids", sorted(previous_hidden & grant))
    if (not isinstance(hidden, list) or any(type(n) is not int or n not in grant for n in hidden)
            or len(set(hidden)) != len(hidden)):
        raise errors.UserError("只能隐藏已授权节点的地址", "hidden_node_ids")
    hidden = set(hidden)
    if old:
        check_hidden_entries(c, ident, hidden - previous_hidden)
    new_password = body.get("password")
    reset = new_password is not None or not old
    hashed = security.hash_password(password(new_password)) if reset else old["password_hash"]
    max_configs = quotas.optional_limit(body.get("max_configs", old["max_configs"] if old else None),
                                       quotas.MAX_CONFIGS, "max_configs", "配置数量上限")
    expires = body.get("expires_at", old["expires_at"] if old else None)
    if expires is not None and (type(expires) is not int or not now < expires <= 253402300799):
        raise errors.UserError("请选择将来的到期时间，或留空长期有效", "expires_at")
    if not old:
        ident = c.execute(
            "INSERT INTO accounts(username,password_hash,enabled,max_configs,expires_at) VALUES (?,?,?,?,?)",
            (name, hashed, enabled, max_configs, expires),
        ).lastrowid
        c.executemany(
            "INSERT INTO item_groups(kind,name,builtin,account_id) VALUES (?,?,1,?)",
            [(kind, settings.DEFAULT_GROUP, ident) for kind in settings.KINDS if kind != "nodes"],
        )
    else:
        c.execute("UPDATE accounts SET username=?,password_hash=?,enabled=?,max_configs=?,expires_at=?,"
                  "revision=revision+1 WHERE id=?", (name, hashed, enabled, max_configs, expires, ident))
        if reset or not enabled or old["username"] != name:
            c.execute("DELETE FROM sessions WHERE account_id=?", (ident,))
        if not enabled and old["enabled"]:
            # Previously shared links require an explicit re-enable and a fresh token.
            for row in c.execute("SELECT id FROM subscriptions WHERE account_id=?", (ident,)).fetchall():
                c.execute("UPDATE subscriptions SET enabled=0,token=? WHERE id=?", (secrets.token_hex(32), row[0]))
        removed = previous - grant
        if (not enabled and old["enabled"]) or removed:
            suspend(c, ident, None if not enabled else removed)
    c.executemany("DELETE FROM account_nodes WHERE account_id=? AND node_id=?", [(ident, n) for n in previous - grant])
    c.executemany("INSERT INTO account_nodes(account_id,node_id,hide_addresses) VALUES (?,?,?)",
                  [(ident, n, int(n in hidden)) for n in grant - previous])
    c.executemany("UPDATE account_nodes SET hide_addresses=? WHERE account_id=? AND node_id=?",
                  [(int(n in hidden), ident, n) for n in (previous_hidden ^ hidden) & grant & previous])
    if "node_limits" in body:
        quotas.update_nodes(c, ident, grant, body["node_limits"])
    return dict(id=ident)


def check_hidden_entries(c, account_id, hidden):
    if not hidden:
        return
    for link in topologies(c, account_id):
        in_graph = not link["deleted"] and any(
            v["kind"] == "entry" and v.get("node_id") in hidden
            for v in json.loads(link["graph"])["vertices"]
        )
        in_flight = False
        for config in c.execute(
            "SELECT f.desired,f.applied,j.payload FROM configurations f "
            "LEFT JOIN jobs j ON j.configuration_id=f.id WHERE f.topology_id=?", (link["id"],),
        ):
            for key in ("desired", "applied", "payload"):
                if not config[key]:
                    continue
                data = json.loads(config[key])
                if (data.get("plan", data).get("entry") in hidden
                        or data.get("old") and data["old"][0]["node_id"] in hidden):
                    in_flight = True
                    break
            if in_flight:
                break
        if in_graph or in_flight:
            raise errors.UserError(
                f"链路「{link['name']}」仍将该节点用作入口，请更换入口并等待同步后再隐藏",
                "hidden_node_ids", status=409,
            )


def suspend(c, account_id, removed):
    """Withdraw staged and active rules directly, without rolling old ingress back in."""
    import execution

    before = node_sync.mutation_snapshot(c)
    for row in list(topologies(c, account_id)):
        if row["suspended"]:
            continue
        tid = row["id"]
        referenced = {v.get("node_id") for v in json.loads(row["graph"])["vertices"]}
        rules = {}
        for config in c.execute("SELECT id FROM configurations WHERE topology_id=?", (tid,)):
            cid = config["id"]
            job = c.execute("SELECT * FROM jobs WHERE configuration_id=?", (cid,)).fetchone()
            rules[cid] = execution.possibly_live_rules(c, cid, job)
            referenced.update(r["node_id"] for r in rules[cid])
            if job:
                referenced.update(r["node_id"] for r in json.loads(job["payload"])["new"])
        if removed is not None and not referenced.intersection(removed):
            continue
        for cid, installed in rules.items():
            execution.withdraw(c, cid, installed)
        c.execute("UPDATE topologies SET suspended=1,revision=revision+1 WHERE id=?", (tid,))
    return node_sync.finish_mutation(c, before)


def start_deletion(c, uid):
    c.execute("UPDATE accounts SET enabled=0,expiring=1,revision=revision+1 WHERE id=?", (uid,))
    c.execute("DELETE FROM sessions WHERE account_id=?", (uid,))
    c.execute("DELETE FROM subscriptions WHERE account_id=?", (uid,))
    changed = suspend(c, uid, None)
    c.execute("DELETE FROM account_nodes WHERE account_id=?", (uid,))
    for link in list(topologies(c, uid)):
        tid = link["id"]
        c.execute("DELETE FROM probe_bindings WHERE scope='topologies' AND owner_id=?", (tid,))
        c.execute("UPDATE topologies SET deleted=1 WHERE id=?", (tid,))
        c.execute("UPDATE configurations SET deleted=1,desired=NULL,state='deleting' WHERE topology_id=?", (tid,))
    return changed


def delete(c, body, ident):
    account = child(c, ident)
    if not account["expiring"]:
        check_revision(account, body)
        start_deletion(c, ident)
    return {}


def expire_due(c, now):
    due = c.execute("SELECT id FROM accounts WHERE expiring=0 AND expires_at<=?", (now,)).fetchall()
    changed = False
    for row in due:
        changed = start_deletion(c, row["id"]) or changed
    return changed


def finish_expirations(c):
    # The executor deletes a topology only after its last rule retirement has been acknowledged.
    ready = c.execute(
        "SELECT a.id FROM accounts a WHERE a.expiring=1 AND NOT EXISTS (SELECT 1 FROM topologies t "
        "JOIN item_groups g ON g.id=t.group_id WHERE g.account_id=a.id)"
    ).fetchall()
    for row in ready:
        uid = row["id"]
        for table in ("targets", "external_configurations"):
            c.execute(f"DELETE FROM {table} WHERE group_id IN (SELECT id FROM item_groups WHERE account_id=?)", (uid,))
        c.execute("DELETE FROM item_groups WHERE account_id=?", (uid,))
        c.execute("DELETE FROM accounts WHERE id=?", (uid,))
