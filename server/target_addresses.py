"""Reachable endpoints of one target, with address choices owned by each edge."""

import json
import re
import database
import errors
import validation


def listing(target):
    return [dict(id="main", name="默认", host=target["host"], port=target["port"])] + json.loads(target["addresses"])


def selected(plan):
    path = plan["graph_path"]
    return plan["graph"]["bindings"].get("/".join(path[-2:]), {}).get("to", {}).get(path[-1]) or "main"


def resolve(c, plan, *, current=False):
    target = dict(database.get_target(c, plan["target"]))
    ident = selected(plan)
    book = {a["id"]: a for a in listing(target)} if current else plan["graph"].get("address_book", {}).get(plan["graph_path"][-1], {})
    # RC1/RC2 snapshots predate target address bindings and use the imported address.
    if not book and ident == "main":
        return target
    if ident not in book:
        raise errors.UserError("目标地址已不存在，请重新选择", "addresses")
    return dict(target, host=book[ident]["host"], port=book[ident]["port"])


def save(c, target, values):
    if not isinstance(values, list) or len(values) > 32:
        raise errors.UserError("每个目标最多添加 32 个接入地址", "addresses")
    result, ids, endpoints = [], {"main"}, {(target["host"], target["port"])}
    for item in values:
        if not isinstance(item, dict):
            raise errors.UserError("目标地址格式无效", "addresses")
        ident = item.get("id")
        if not isinstance(ident, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", ident) or ident in ids:
            raise errors.UserError("目标地址标识无效或重复", "addresses")
        host = validation.check_host(item.get("host"), "addresses")
        port = validation.check_port(item.get("port"), "addresses")
        if (host, port) in endpoints:
            raise errors.UserError("目标接入地址重复", "addresses")
        name = validation.check_remark(item["name"], "addresses", "地址名称") if item.get("name") else ""
        result.append(dict(id=ident, name=name, host=host, port=port))
        ids.add(ident)
        endpoints.add((host, port))
    removed = {a["id"] for a in listing(target)} - ids
    if removed:
        import topology
        import node_addresses
        for name, doc in topology.documents(c):
            graph = doc.get("graph", {})
            used = node_addresses.references(graph)
            if any(v.get("target_id") == target["id"] and used.get(v["id"], set()) & removed
                   for v in graph.get("vertices", [])):
                raise errors.UserError(f"目标地址仍被「{name}」引用，请先替换连线并等待同步完成", "addresses")
    c.execute("UPDATE targets SET addresses=? WHERE id=?", (json.dumps(result, ensure_ascii=False), target["id"]))


def refresh(c, old, target):
    """Update designs and desired plans, keeping applied/in-flight receipts intact."""
    import execution
    book = {a["id"]: a for a in listing(target)}
    previous = {a["id"]: a for a in listing(old)}
    changed = {i for i, a in book.items() if i not in previous or
               (a["host"], a["port"]) != (previous[i]["host"], previous[i]["port"])}

    def update(graph):
        dirty = False
        for v in graph["vertices"]:
            if v.get("target_id") == target["id"] and graph["address_book"].get(v["id"]) != book:
                graph["address_book"][v["id"]] = book
                dirty = True
        return dirty

    for row in c.execute("SELECT id,graph FROM topologies").fetchall():
        graph = json.loads(row["graph"])
        if update(graph):
            c.execute("UPDATE topologies SET graph=?,revision=revision+1 WHERE id=?", (execution.dump(graph), row["id"]))
    for row in c.execute("SELECT id,desired FROM configurations WHERE desired IS NOT NULL").fetchall():
        plan = json.loads(row["desired"])
        if plan["target"] != target["id"] or not update(plan["graph"]):
            continue
        c.execute("UPDATE configurations SET desired=?,revision=revision+1 WHERE id=?", (execution.dump(plan), row["id"]))
        if selected(plan) in changed:
            c.execute("UPDATE configurations SET state='pending',error='',failover='{}' WHERE id=? AND deleted=0", (row["id"],))
