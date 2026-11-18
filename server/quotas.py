"""Per-account configuration limits and per-node forwarding allowances."""

import errors

GB = 1_000_000_000
MAX_CONFIGS = 1_000_000
MAX_GB = 1_000_000_000


def optional_limit(value, maximum, field, label):
    if value is not None and (type(value) is not int or not 0 <= value <= maximum):
        raise errors.UserError(f"{label}须为 0～{maximum} 的整数，或留空", field)
    return value


def config_count(c, account_id):
    return c.execute(
        "SELECT (SELECT COUNT(*) FROM configurations f "
        "JOIN topologies t ON t.id=f.topology_id JOIN item_groups g ON g.id=t.group_id "
        "WHERE g.account_id=? AND f.deleted=0 AND t.deleted=0) + "
        "(SELECT COUNT(*) FROM external_configurations e JOIN item_groups g ON g.id=e.group_id WHERE g.account_id=?)",
        (account_id, account_id),
    ).fetchone()[0]


def config_limit(c, account_id):
    if account_id is None:
        return None
    return c.execute("SELECT max_configs FROM accounts WHERE id=?", (account_id,)).fetchone()[0]


def check_config_limit(limit, before, after):
    if limit is not None and after > limit and after > before:
        raise errors.UserError(f"配置数量将达 {after} 项，超过账户上限 {limit} 项", "max_configs", 409)


def used_bytes(row):
    return int(row["total"]) - int(row["baseline"] or "0")


def node_view(c, account_id):
    result = {}
    for row in c.execute("SELECT * FROM account_node_traffic WHERE account_id=?", (account_id,)):
        used, limit = used_bytes(row), row["limit_gb"]
        remaining = limit * GB - used if limit is not None else None
        result[row["node_id"]] = dict(
            limit_gb=limit, used=str(used),
            remaining=str(max(0, remaining)) if remaining is not None else None,
            exhausted=remaining is not None and remaining <= 0,
        )
    return result


def update_nodes(c, account_id, grants, updates):
    if not isinstance(updates, list) or len(updates) > len(grants):
        raise errors.UserError("流量额度列表无效", "node_limits")
    available = {r[0] for r in c.execute("SELECT id FROM nodes WHERE desired_mode='active' AND deleting=0")}
    seen = set()
    for item in updates:
        if (not isinstance(item, dict) or type(item.get("node_id")) is not int
                or type(item.get("reset", False)) is not bool or "limit_gb" not in item):
            raise errors.UserError("流量额度格式无效", "node_limits")
        nid = item["node_id"]
        if nid not in grants or nid not in available or nid in seen:
            raise errors.UserError("只能为已获授权的主动节点设置流量额度", "node_limits")
        seen.add(nid)
        limit = optional_limit(item["limit_gb"], MAX_GB, "node_limits", "流量额度（GB）")
        row = c.execute("SELECT * FROM account_node_traffic WHERE account_id=? AND node_id=?",
                        (account_id, nid)).fetchone()
        total = row["total"] if row else "0"
        baseline = row["baseline"] if row else None
        if item.get("reset") or (limit is not None and baseline is None):
            baseline = total
        c.execute(
            "INSERT INTO account_node_traffic(account_id,node_id,limit_gb,total,baseline) VALUES (?,?,?,?,?) "
            "ON CONFLICT(account_id,node_id) DO UPDATE SET limit_gb=excluded.limit_gb,baseline=excluded.baseline",
            (account_id, nid, limit, total, baseline),
        )


def register_rules(c, account_id, rules):
    if account_id is not None:
        c.executemany("INSERT OR IGNORE INTO traffic_rule_owners VALUES (?,?,?)",
                      [(r["id"], r["node_id"], account_id) for r in rules])


def add_usage(c, nid, deltas):
    for account_id, delta in deltas.items():
        row = c.execute("SELECT total FROM account_node_traffic WHERE account_id=? AND node_id=?",
                        (account_id, nid)).fetchone()
        total = (int(row["total"]) if row else 0) + delta
        c.execute(
            "INSERT INTO account_node_traffic(account_id,node_id,total) VALUES (?,?,?) "
            "ON CONFLICT(account_id,node_id) DO UPDATE SET total=excluded.total",
            (account_id, nid, str(total)),
        )


def exhausted_nodes(c):
    result = {}
    for r in c.execute(
        "SELECT q.* FROM account_node_traffic q JOIN account_nodes a "
        "ON a.account_id=q.account_id AND a.node_id=q.node_id "
        "JOIN nodes n ON n.id=q.node_id WHERE q.limit_gb IS NOT NULL AND n.desired_mode='active'"
    ):
        if used_bytes(r) >= r["limit_gb"] * GB:
            result.setdefault(r["account_id"], set()).add(r["node_id"])
    return result
