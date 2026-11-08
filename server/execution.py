"""Per-configuration two-phase execution. Saving topology never runs this code."""

import asyncio
import copy
import json
import logging
import time
import agent_v3
import database
import errors
import forwarding
import node_addresses
import node_sync
import settings
import topology
import validation


def dump(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def get(c, ident):
    row = c.execute("SELECT * FROM configurations WHERE id=?", (ident,)).fetchone()
    if row is None:
        raise errors.UserError("配置不存在", status=404)
    return dict(row)


def runtime_plan(config):
    return json.loads(config["applied"])["plan"] if config["applied"] else None


def applied_rules_present(c, config):
    if not config["applied"]:
        return False
    fields = ("id", "node_id", "port", "host", "tport", "route")
    actual = {
        tuple(r[k] for k in fields)
        for r in c.execute("SELECT * FROM rules WHERE configuration_id=?", (config["id"],))
    }
    return all(
        tuple(r.get(k, "") for k in fields) in actual
        for r in json.loads(config["applied"])["rules"]
    )


def applied_target(c, config):
    target = json.loads(config["applied"])["target"]
    current = c.execute("SELECT * FROM targets WHERE id=?", (target["id"],)).fetchone()
    return (
        dict(current)
        if current and (current["host"], current["port"]) == (target["host"], target["port"])
        else target
    )


def event(c, ident, message):
    c.execute(
        "INSERT INTO configuration_events(configuration_id,ts,message) VALUES (?,?,?)",
        (ident, int(time.time()), message),
    )


def operational(plan):
    if not plan:
        return None
    g = plan["graph"]
    path = plan["graph_path"]
    by = {v["id"]: v for v in g["vertices"]}
    result = []
    for i, vid in enumerate(path[:-1]):
        v = by[vid]
        incoming = (
            v.get("address") if i == 0 else g["bindings"][path[i - 1] + "/" + vid]["to"][vid]
        )
        outgoing = g["bindings"][vid + "/" + path[i + 1]]["from"][vid]
        book = g["address_book"][vid]
        result.append(
            (
                v["node_id"],
                book[incoming]["host"],
                None if outgoing == "@auto" else node_addresses.routing(book[outgoing]),
            )
        )
    return result, plan["target"], plan.get("port")


def confirmed(c, nodes):
    for nid in nodes:
        n = database.get_node(c, nid)
        if (
            n["manual_required"]
            or n["state"] != "ok"
            or n["applied_generation"] != n["desired_generation"]
            or n["reported_v"] != forwarding.rules_version(forwarding.node_rules(c, n))
        ):
            return False
    return True


def ready(c, nid, now):
    n = database.get_node(c, nid)
    if n["deleting"] or n["manual_required"]:
        raise errors.WaitingError(f"{n['name']} 等待本机确认")
    kind = n["desired_mode"]
    if n["installed_agent"] != kind or agent_v3.newer(
        settings.COMPONENTS[kind + "_agent"], n["agent"] or "0.0.0"
    ):
        raise errors.WaitingError(f"{n['name']} 等待 Agent 升级")
    if c.execute("SELECT 1 FROM migration_nodes WHERE node_id=?", (nid,)).fetchone():
        raise errors.WaitingError(f"{n['name']} 等待清理旧配置")
    if kind == "active" and (n["last_seen"] is None or now - n["last_seen"] >= 30):
        raise errors.WaitingError(f"{n['name']} 离线，等待连接")
    if n["applied_generation"] != n["desired_generation"]:
        raise errors.WaitingError(f"{n['name']} 正在同步其他配置")
    if n["state"] == "error":
        raise errors.WaitingError(f"{n['name']}：{n['err'] or '节点异常'}")
    return n


def put_rule(c, ident, r, phase):
    c.execute(
        "INSERT INTO rules VALUES (?,?,?,?,?,?,?,?)",
        (
            r["id"],
            ident,
            phase,
            r["node_id"],
            r["port"],
            r["host"],
            r["tport"],
            r.get("route", ""),
        ),
    )


def retire(c, ident, rules):
    for r in rules:
        c.execute("DELETE FROM rules WHERE id=?", (r["id"],))
        # A reused ingress port still belongs to the replacement rule.
        if c.execute(
            "SELECT 1 FROM rules WHERE node_id=? AND port=?", (r["node_id"], r["port"])
        ).fetchone():
            continue
        c.execute("INSERT OR IGNORE INTO retired_ports VALUES (?,?)", (r["node_id"], r["port"]))
        c.execute(
            "INSERT OR IGNORE INTO retirements VALUES (?,?,?)", (ident, r["node_id"], r["port"])
        )


def make_rules(c, config, plan, now):
    plan = copy.deepcopy(plan)
    g = plan["graph"]
    path = plan["graph_path"]
    by = {v["id"]: v for v in g["vertices"]}
    wanted = {by[i]["node_id"] for i in path[:-1]}
    applied = json.loads(config["applied"]) if config["applied"] else None
    if applied:
        wanted.update(r["node_id"] for r in applied["rules"])
    for row in c.execute("SELECT payload FROM jobs"):
        other = json.loads(row[0])
        if wanted & {r["node_id"] for r in other["new"] + other["old"]}:
            raise errors.WaitingError("共享节点正在同步其他配置")
    nodes = [ready(c, by[i]["node_id"], now) for i in path[:-1]]
    for vid in path[:-1]:
        out = g["bindings"][vid + "/" + path[path.index(vid) + 1]]["from"][vid]
        if out == "@auto":
            continue
        n = by[vid]["node_id"]
        snapshot = node_addresses.network(c, n)
        address = g["address_book"][vid][out]
        # Resolve a selected source from the latest inventory only during execution.
        resolved = copy.deepcopy(address)
        node_addresses.resolve(resolved, snapshot["addresses"])
        reason = node_addresses.egress_problem(resolved, snapshot)
        if reason:
            error = errors.WaitingError if not snapshot["complete"] else errors.UserError
            raise error(f"{database.get_node(c,n)['name']}：{reason}")
        g["address_book"][vid][out] = resolved
    plan.update(topology.compile_routing(g, path))
    target = dict(database.get_target(c, plan["target"]))
    applied = json.loads(config["applied"]) if config["applied"] else None
    old = applied["rules"] if applied else []
    rules = []
    for i, n in enumerate(nodes):
        existing = old[0] if i == 0 and old and old[0]["node_id"] == n["id"] else None
        port = (
            (plan.get("port") or (existing["port"] if existing else None)) if i == 0 else None
        )
        used = forwarding.reserved(c, n["id"], config["id"] if i == 0 else None)
        if n["quality_port"]:
            used.add(n["quality_port"])
        if n["wake_enabled"]:
            used.add(n["wake_port"])
        ranges = validation.parse_ports(n["ports"])
        if port is None:
            port = validation.pick_port(ranges, used)
        if (
            port in used
            and c.execute(
                "SELECT 1 FROM retired_ports WHERE node_id=? AND port=?", (n["id"], port)
            ).fetchone()
        ):
            raise errors.WaitingError(f"{n['name']} 的入口端口等待旧规则或连接清理")
        if port is None or not validation.in_ports(port, ranges) or port in used:
            raise errors.UserError(
                f"{n['name']} 没有可用端口，或入口端口已占用", f"capacity:{n['id']}"
            )
        rid = int(database.setting(c, "next_hop_id"))
        database.set_setting(c, "next_hop_id", str(rid + 1))
        rules.append(dict(id=rid, node_id=n["id"], port=port, route=plan["routing"][i]))
    for i, r in enumerate(rules):
        r.update(
            host=plan["hop_addresses"][i + 1] if i + 1 < len(rules) else target["host"],
            tport=rules[i + 1]["port"] if i + 1 < len(rules) else target["port"],
        )
    plan["port"] = rules[0]["port"]
    return plan, target, old, rules


def queue(c, config, now):
    if config["deleted"]:
        old = [
            dict(r)
            for r in c.execute("SELECT * FROM rules WHERE configuration_id=?", (config["id"],))
        ]
        retire(c, config["id"], old)
        c.execute("UPDATE configurations SET state='deleting' WHERE id=?", (config["id"],))
        return
    if not config["desired"] or config["state"] == "error":
        return
    if config["applied"] and not applied_rules_present(c, config):
        # A restored design may refer to a snapshot already removed from the ledger.
        retire(
            c,
            config["id"],
            [
                dict(r)
                for r in c.execute(
                    "SELECT * FROM rules WHERE configuration_id=?", (config["id"],)
                )
            ],
        )
        c.execute("UPDATE configurations SET applied=NULL WHERE id=?", (config["id"],))
        config = dict(config, applied=None)
    plan = json.loads(config["desired"])
    current = runtime_plan(config)
    comparison = copy.deepcopy(plan)
    if current and comparison.get("port") is None:
        comparison["port"] = current.get("port")
    target = database.get_target(c, plan["target"])
    previous_intent = json.loads(config["applied"]).get("intent", current) if current else None
    if (
        current
        and operational(comparison) in (operational(current), operational(previous_intent))
        and (target["host"], target["port"])
        == (applied_target(c, config)["host"], applied_target(c, config)["port"])
    ):
        applied = json.loads(config["applied"])
        # Reflect presentation/vertex changes while preserving the confirmed wire settings.
        projected = copy.deepcopy(plan)
        for key in ("port", "routing", "hop_addresses", "ingress_host"):
            projected[key] = current[key]
        for index, vid in enumerate(projected["graph_path"][:-1]):
            old_vid = current["graph_path"][index]
            old_next = current["graph_path"][index + 1]
            next_vid = projected["graph_path"][index + 1]
            source = projected["graph"]["bindings"][vid + "/" + next_vid]["from"][vid]
            old_source = current["graph"]["bindings"][old_vid + "/" + old_next]["from"][old_vid]
            if source != "@auto" and old_source != "@auto":
                actual = current["graph"]["address_book"][old_vid][old_source]
                for key in ("local", "device", "gateway"):
                    projected["graph"]["address_book"][vid][source][key] = actual[key]
        applied.update(plan=projected, intent=plan)
        if config["state"] != "active" or config["error"] or dump(applied) != config["applied"]:
            c.execute(
                "UPDATE configurations SET applied=?,state='active',error='' WHERE id=?",
                (dump(applied), config["id"]),
            )
        return
    try:
        with database.atomic(c):
            actual, target, old, new = make_rules(c, config, plan, now)
            for r in new[1:]:
                put_rule(c, config["id"], r, "stage")
            job = dict(
                plan=actual,
                target=target,
                old=old,
                new=new,
                desired=config["desired"],
                wait=sorted({r["node_id"] for r in new[1:]}),
            )
            c.execute(
                "INSERT INTO jobs VALUES (?,?,?,?)", (config["id"], "prepare", dump(job), now)
            )
            c.execute(
                "UPDATE configurations SET state='preparing',error='' WHERE id=?",
                (config["id"],),
            )
            event(c, config["id"], "准备下游规则")
    except errors.UserError as exc:
        # Availability is a resumable wait. Allocation and invalid source settings need correction.
        waiting = isinstance(exc, errors.WaitingError)
        c.execute(
            "UPDATE configurations SET state=?,error=? WHERE id=?",
            ("waiting" if waiting else "error", str(exc), config["id"]),
        )


def failed(c, nodes):
    return any(
        c.execute(
            "SELECT 1 FROM sync_ops o JOIN nodes n ON n.id=o.node_id WHERE n.id=? AND o.generation=n.desired_generation AND o.result='failed'",
            (nid,),
        ).fetchone()
        for nid in nodes
    )


def progress(c, config, row, now):
    ident = config["id"]
    phase = row["phase"]
    job = json.loads(row["payload"])
    desired = json.loads(config["desired"]) if config["desired"] else None
    target = database.get_target(c, desired["target"]) if desired else None
    superseded = bool(
        config["deleted"]
        or operational(desired) != operational(json.loads(job["desired"]))
        or target
        and (target["host"], target["port"]) != (job["target"]["host"], job["target"]["port"])
    )
    if phase == "prepare" and (superseded or failed(c, job["wait"])):
        retire(c, ident, job["new"][1:])
        c.execute("DELETE FROM jobs WHERE configuration_id=?", (ident,))
        c.execute(
            "UPDATE configurations SET state=?,error=? WHERE id=?",
            (
                "pending" if superseded else "error",
                "" if superseded else "下游准备失败，保留原路径",
                ident,
            ),
        )
        return
    if not confirmed(c, job["wait"]):
        if phase == "commit" and failed(c, job["wait"]):
            # Restore the previous ingress before retiring newly prepared rules.
            for r in job["new"][:1]:
                c.execute("DELETE FROM rules WHERE id=?", (r["id"],))
            for r in job["old"][:1]:
                if not c.execute("SELECT 1 FROM rules WHERE id=?", (r["id"],)).fetchone():
                    put_rule(c, ident, r, "live")
            job["wait"] = sorted({r["node_id"] for r in job["new"][:1] + job["old"][:1]})
            c.execute(
                "UPDATE jobs SET phase=?,payload=?,started=? WHERE configuration_id=?",
                ("rollback", dump(job), now, ident),
            )
        if now - row["started"] >= 30:
            c.execute(
                "UPDATE configurations SET state='waiting',error='等待节点同步确认' WHERE id=?",
                (ident,),
            )
        return
    if phase == "rollback":
        retire(c, ident, job["new"])
        c.execute("DELETE FROM jobs WHERE configuration_id=?", (ident,))
        c.execute(
            "UPDATE configurations SET state=?,error=? WHERE id=?",
            ("pending" if superseded else "error", "入口更新失败，原路径已恢复", ident),
        )
        return
    if phase == "prepare":
        entry = job["new"][0]
        for old in job["old"]:
            if (old["node_id"], old["port"]) == (entry["node_id"], entry["port"]):
                c.execute("DELETE FROM rules WHERE id=?", (old["id"],))
        put_rule(c, ident, entry, "stage")
        job["wait"] = [entry["node_id"]]
        c.execute(
            "UPDATE jobs SET phase=?,payload=?,started=? WHERE configuration_id=?",
            ("commit", dump(job), now, ident),
        )
        c.execute("UPDATE configurations SET state='switching',error='' WHERE id=?", (ident,))
        return
    new_ids = {r["id"] for r in job["new"]}
    retire(c, ident, [r for r in job["old"] if r["id"] not in new_ids])
    c.execute("UPDATE rules SET phase='live' WHERE configuration_id=?", (ident,))
    c.execute(
        "UPDATE configurations SET applied=?,state=?,error='' WHERE id=?",
        (
            dump(
                dict(
                    plan=job["plan"],
                    target=job["target"],
                    rules=job["new"],
                    intent=json.loads(job["desired"]),
                )
            ),
            "pending" if superseded else "active",
            ident,
        ),
    )
    c.execute("DELETE FROM jobs WHERE configuration_id=?", (ident,))
    event(c, ident, "路径已确认生效")


def tick(c, now=None):
    now = int(time.time()) if now is None else now
    changed = False
    for r in c.execute("SELECT * FROM retirements").fetchall():
        if (
            confirmed(c, [r["node_id"]])
            and not c.execute(
                "SELECT 1 FROM retired_ports WHERE node_id=? AND port=?",
                (r["node_id"], r["port"]),
            ).fetchone()
        ):
            c.execute(
                "DELETE FROM retirements WHERE configuration_id=? AND node_id=? AND port=?",
                tuple(r),
            )
    for config in [
        dict(r) for r in c.execute("SELECT * FROM configurations ORDER BY id").fetchall()
    ]:
        before = node_sync.mutation_snapshot(c)
        job = c.execute(
            "SELECT * FROM jobs WHERE configuration_id=?", (config["id"],)
        ).fetchone()
        if job:
            progress(c, config, job, now)
        else:
            queue(c, config, now)
        changed = node_sync.finish_mutation(c, before) or changed
        if (
            config["deleted"]
            and not c.execute(
                "SELECT 1 FROM jobs WHERE configuration_id=?", (config["id"],)
            ).fetchone()
            and not c.execute(
                "SELECT 1 FROM rules WHERE configuration_id=?", (config["id"],)
            ).fetchone()
            and not c.execute(
                "SELECT 1 FROM retirements WHERE configuration_id=?", (config["id"],)
            ).fetchone()
        ):
            c.execute("DELETE FROM configurations WHERE id=?", (config["id"],))
    c.execute(
        "DELETE FROM topologies WHERE deleted=1 AND id NOT IN (SELECT topology_id FROM configurations)"
    )
    return changed


def retry(c, ident):
    config = get(c, ident)
    row = c.execute("SELECT * FROM jobs WHERE configuration_id=?", (ident,)).fetchone()
    if row:
        job = json.loads(row["payload"])
        node_sync.retry_nodes(c, job["wait"])
        c.execute(
            "UPDATE jobs SET started=? WHERE configuration_id=?", (int(time.time()), ident)
        )
    else:
        plan = json.loads(config["desired"]) if config["desired"] else None
        nodes = (
            [v["node_id"] for v in plan["graph"]["vertices"] if v.get("node_id")]
            if plan
            else []
        )
        nodes.extend(
            r[0]
            for r in c.execute(
                "SELECT DISTINCT node_id FROM retirements WHERE configuration_id=?", (ident,)
            )
        )
        node_sync.retry_nodes(
            c, [nid for nid in nodes if database.get_node(c, nid)["state"] == "error"]
        )
        c.execute(
            "UPDATE configurations SET state=?,error='' WHERE id=?",
            ("deleting" if config["deleted"] else "pending", ident),
        )


class Monitor:
    def __init__(self, hub):
        self.hub = hub
        self.task = None

    async def run(self):
        while True:
            try:
                if await self.hub.database(tick):
                    self.hub.notify()
            except asyncio.CancelledError:
                raise
            except Exception:
                logging.exception("configuration execution")
            await asyncio.sleep(1)

    def start(self):
        self.task = asyncio.create_task(self.run())

    async def stop(self):
        if self.task:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
