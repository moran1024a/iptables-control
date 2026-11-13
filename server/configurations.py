"""Saved topology and its reachable entry/target configurations."""

import copy
import json
import time
import database
import errors
import execution
import forwarding
import node_addresses
import node_tags
import node_state
import target_config
import target_addresses
import topology
import validation
import external_configurations


def get_topology(c, ident):
    r = c.execute("SELECT * FROM topologies WHERE id=?", (ident,)).fetchone()
    if not r:
        raise errors.UserError("拓扑不存在", status=404)
    return dict(r)


def members(c, ident):
    return [
        dict(r)
        for r in c.execute(
            "SELECT * FROM configurations WHERE topology_id=? ORDER BY id", (ident,)
        )
    ]


def code(ident):
    return f"C{ident:04d}"


def order(c):
    ranks = {r["id"]: i for i, r in enumerate(c.execute(database.ordered("topologies")))}
    return lambda item: (ranks.get(item["topology_id"], 0), item["id"])


def plans(c, graph, existing=(), prefix=(), required=(), entry=None):
    current = {
        (r["entry"], r["target"]): json.loads(r["desired"])
        for r in existing
        if r["desired"] and not r["deleted"]
    }
    applied = {
        (r["entry"], r["target"]): json.loads(r["applied"])["plan"]
        for r in existing if r["applied"] and not r["deleted"]
    }
    result = []
    for (a, b), fallback in topology.pairs(graph).items():
        if entry and a != entry:
            continue
        old = current.get((a, b), {})
        preferred = old.get("graph_path", [])
        prefixes = [prefix]
        if required and not prefix:
            # A quick click changes the nearest necessary branch of the confirmed
            # route. Weight only ranks alternatives after that retained prefix.
            preferred = applied.get((a, b), old).get("graph_path", [])
            prefixes = [preferred[:i] for i in range(len(preferred), 0, -1)] + [[]]
        path = None
        for candidate in prefixes:
            path = topology.find_path(graph, entry=a, target=b, prefix=candidate,
                                      required=required, preferred=preferred)
            if path:
                break
        if path:
            result.append(
                dict(entry=a, target=b, path=path, unchanged=path == old.get("graph_path"))
            )
    return result


def preview(c, body):
    graph = topology.validate(c, body.get("graph"), complete=not body.get("layout_only"))
    if body.get("layout_only"):
        return dict(graph=topology.arrange(graph))
    prefix, required = body.get("prefix", []), body.get("required", [])
    if any(
        not isinstance(x, list) or any(not isinstance(i, str) for i in x)
        for x in (prefix, required)
    ):
        topology.fail("无效路径选择")
    if body.get("id") is not None and (type(body["id"]) is not int or not 0 < body["id"] < 2**63):
        topology.fail("无效的拓扑 ID")
    existing = members(c, body["id"]) if body.get("id") else []
    entry = body.get("entry") or (prefix[0] if prefix else None)
    selected = plans(c, graph, existing, prefix, required, entry)
    choices = []
    if prefix:
        kinds = {v["id"]: v["kind"] for v in graph["vertices"]}
        choices = [
            b
            for a, b in graph["edges"]
            if a == prefix[-1]
            and (kinds[b] == "node" or kinds[b] == "direct"
                 and any(x == b and kinds[y] == "node" for x, y in graph["edges"]))
            and plans(c, graph, existing, prefix + [b], required, entry)
        ]
    return dict(
        graph=topology.arrange(graph) if body.get("arrange") else graph,
        paths=selected,
        path=selected[0]["path"] if selected else [],
        prefix=prefix,
        options=choices,
        complete=bool(prefix and not choices),
        skipped=[],
        warnings=warnings(graph),
    )


def checked_paths(graph, paths):
    if not isinstance(paths, list) or len(paths) > 64:
        topology.fail("路径列表无效")
    result = [topology.check_path(graph, p) for p in paths]
    if len({(p[0], p[-1]) for p in result}) != len(result):
        topology.fail("每对入口和目标只能选择一条路径")
    return result


def entry_port(c, graph, vertex, port, prior):
    node = database.get_node(
        c, next(v["node_id"] for v in graph["vertices"] if v["id"] == vertex)
    )
    used = forwarding.reserved(
        c, node["id"], prior["id"] if prior else None, allow_owned_retired=True
    )
    if node["quality_port"]:
        used.add(node["quality_port"])
    if node["wake_enabled"]:
        used.add(node["wake_port"])
    ranges = validation.parse_ports(node["ports"])
    if port is None:
        port = validation.pick_port(ranges, used)
    if port is None or port in used or not validation.in_ports(port, ranges):
        raise errors.UserError(
            f"{node['name']} 的入口端口不可用，请选择允许范围内的空闲端口", "vertex:" + vertex
        )
    return port


def warnings(graph):
    # Include every vertex on some valid path, not just the chosen shortest paths.
    starts = {v["id"] for v in graph["vertices"] if v["kind"] == "entry"}
    ends = {v["id"] for v in graph["vertices"] if v["kind"] == "target"}

    def reachable(seed, reverse=False):
        seen = set(seed)
        queue = list(seed)
        for v in queue:
            for a, b in graph["edges"]:
                if reverse:
                    a, b = b, a
                if a == v and b not in seen:
                    seen.add(b)
                    queue.append(b)
        return seen

    used = reachable(starts) & reachable(ends, True)
    return [
        dict(vertex=v["id"], message="当前没有可达的入口—目标配置")
        for v in graph["vertices"]
        if v["id"] not in used
    ]


def save_topology(c, body, ident=None):
    old = get_topology(c, ident) if ident is not None else None
    if old and old["deleted"]:
        raise errors.UserError("拓扑正在删除", status=409)
    if old and body.get("revision") != old["revision"]:
        raise errors.UserError("拓扑已变化，请刷新后重试", status=409)
    enabled = body.get("failover_enabled", bool(old["failover_enabled"]) if old else False)
    if type(enabled) is not bool:
        raise errors.UserError("自动灾备开关无效", "failover_enabled")
    graph = topology.validate(c, body.get("graph"))
    existing = members(c, ident) if ident else []
    if body.get("switch_only"):
        if not old:
            topology.fail("请先保存拓扑")
        if topology.snapshot(graph) != topology.snapshot(topology.validate(c, json.loads(old["graph"]))):
            raise errors.UserError("拓扑已变化，请重新选择", status=409)
        selected = checked_paths(graph, body.get("paths", []))
        changed = 0
        for path in selected:
            topology.check_path(graph, path)
            config = next(
                (
                    r
                    for r in existing
                    if not r["deleted"] and (r["entry"], r["target"]) == (path[0], path[-1])
                ),
                None,
            )
            if not config:
                topology.fail("对应配置不存在")
            wanted = json.loads(config["desired"])
            plan = topology.compile_plan(graph, path, wanted.get("port"))
            c.execute("UPDATE configurations SET failover='{}' WHERE id=?", (config["id"],))
            if execution.operational(plan) == execution.operational(wanted) and plan["graph_path"] == wanted["graph_path"]:
                continue
            c.execute(
                "UPDATE configurations SET desired=?,state='pending',error='',revision=revision+1 WHERE id=?",
                (execution.dump(plan), config["id"]),
            )
            changed += 1
        c.execute("UPDATE topologies SET revision=revision+1 WHERE id=?", (ident,))
        return dict(id=ident, changed=changed, skipped=[])
    name = validation.check_remark(body.get("name"), "name", "名称")
    group = database.check_group(
        c, "topologies", body.get("group_id", old["group_id"] if old else None)
    )
    graph = topology.arrange(graph)
    if old:
        c.execute(
            "UPDATE topologies SET name=?,group_id=?,graph=?,revision=revision+1 WHERE id=?",
            (name, group, execution.dump(graph), ident),
        )
    else:
        ident = c.execute(
            "INSERT INTO topologies(name,group_id,graph,sort) VALUES (?,?,?,(SELECT COALESCE(MAX(sort),0)+1 FROM topologies))",
            (name, group, execution.dump(graph)),
        ).lastrowid
    c.execute("UPDATE topologies SET failover_enabled=? WHERE id=?", (int(enabled), ident))
    c.execute("UPDATE configurations SET failover='{}' WHERE topology_id=?", (ident,))
    chosen = plans(c, graph, existing)
    requested = {(p[0], p[-1]): p for p in checked_paths(graph, body.get("paths", []))}
    wanted = set()
    for item in chosen:
        pair = item["entry"], item["target"]
        wanted.add(pair)
        prior = next((r for r in existing if (r["entry"], r["target"]) == pair), None)
        port = graph["ports"].get("/".join(pair))
        if port is None and prior and prior["desired"]:
            port = json.loads(prior["desired"]).get("port")
        port = entry_port(c, graph, pair[0], port, prior)
        graph["ports"]["/".join(pair)] = port
        plan = topology.compile_plan(graph, requested.get(pair, item["path"]), port)
        if prior:
            if (
                prior["deleted"]
                and prior["applied"]
                and not execution.applied_rules_present(c, prior)
            ):
                c.execute("UPDATE configurations SET applied=NULL WHERE id=?", (prior["id"],))
            same = (
                prior["desired"]
                and execution.operational(json.loads(prior["desired"]))
                == execution.operational(plan)
                and not prior["deleted"]
            )
            c.execute(
                "UPDATE configurations SET desired=?,deleted=0,state=?,error=?,revision=revision+1 WHERE id=?",
                (
                    execution.dump(plan),
                    prior["state"] if same else "pending",
                    prior["error"] if same else "",
                    prior["id"],
                ),
            )
        else:
            cid = c.execute(
                "INSERT INTO configurations(topology_id,entry,target,desired) VALUES (?,?,?,?)",
                (ident, *pair, execution.dump(plan)),
            ).lastrowid
            c.execute("UPDATE configurations SET name=? WHERE id=?", (f"配置 {cid:04d}", cid))
    for r in existing:
        if (r["entry"], r["target"]) not in wanted:
            c.execute(
                "UPDATE configurations SET deleted=1,desired=NULL,state='deleting' WHERE id=?",
                (r["id"],),
            )
    c.execute("UPDATE topologies SET graph=? WHERE id=?", (execution.dump(graph), ident))
    return dict(id=ident, configurations=len(chosen), warnings=warnings(graph))


def view(c, r):
    applied = json.loads(r["applied"]) if r["applied"] else None
    desired = json.loads(r["desired"]) if r["desired"] else None
    job = c.execute("SELECT * FROM jobs WHERE configuration_id=?", (r["id"],)).fetchone()
    current = applied["plan"] if applied else None
    state = r["state"]
    error = r["error"]
    if state == "active" and applied:
        states = [
            node_state.node_status(c, database.get_node(c, x["node_id"]), int(time.time()))
            for x in applied["rules"]
        ]
        if any(s in ("error", "offline", "skew") for s in states):
            state = "error"
            error = "路径节点异常或离线"
    plan = current or desired
    target = (
        execution.applied_target(c, r) if current else target_addresses.resolve(c, plan)
    ) if plan and not r["deleted"] else None
    connection = (
        target_config.connection(
            target, current["ingress_host"], current["port"], r["name"]
        )
        if current and not r["deleted"]
        else None
    )
    return dict(
        id=r["id"],
        entry=r["entry"],
        target=r["target"],
        name=r["name"],
        code=code(r["id"]),
        state=state,
        error=error,
        switching=bool(job),
        active_path=current["graph_path"] if current else [],
        pending_path=desired["graph_path"] if desired and state != "active" else [],
        active_graph=current["graph"] if current else None,
        pending_graph=desired["graph"] if desired and state != "active" else None,
        port=current.get("port") if current else desired.get("port") if desired else None,
        connection=connection,
        target_address=target_addresses.describe(c, plan, target) if target else None,
        failover=json.loads(r["failover"]),
    )


def resources(c):
    return dict(
        groups=database.get_groups(c, "topologies"),
        node_groups=database.get_groups(c, "nodes"),
        target_groups=database.get_groups(c, "targets"),
        nodes=[
            dict(
                id=n["id"],
                name=n["name"],
                addr=n["addr"],
                weight=n["weight"],
                group_id=n["group_id"],
                tags=node_tags.listing(c, n["id"]),
                addresses=node_addresses.listing(c, n),
                network=node_addresses.network(c, n["id"]),
                mode=n["desired_mode"],
                status=node_state.node_status(c, n, int(time.time())),
            )
            for n in c.execute(database.ordered("nodes"))
        ],
        targets=[dict(r, addresses=target_addresses.listing(r)) for r in c.execute(database.ordered("targets"))],
    )


def detail(c, ident):
    r = get_topology(c, ident)
    configs = members(c, ident)
    views = [view(c, x) for x in configs]
    graph = json.loads(r["graph"])
    weights = {n["id"]: n["weight"] for n in c.execute("SELECT id,weight FROM nodes")}
    for vertex in graph["vertices"]:
        if vertex.get("node_id"):
            vertex["weight"] = weights.get(vertex["node_id"], 0)
    display = copy.deepcopy(graph)
    # Keep only the route snapshots needed to show still-live removed vertices.
    vertices = {v["id"]: v for v in display["vertices"]}
    aliases = {}

    def identity(v):
        return v["kind"], v.get("node_id"), v.get("target_id")

    def height(v):
        if v["kind"] == "direct":
            return 56
        return 62 + 24 * (
            max(1, len(display["address_book"].get(v["id"], {}))) + bool(v.get("node_id"))
        )

    for item in views:
        if not item["active_graph"]:
            continue
        old = copy.deepcopy(item["active_graph"])
        mapping = {}
        for v in old["vertices"]:
            vid = v["id"]
            if vid in vertices and identity(v) != identity(vertices[vid]):
                mapping[vid] = aliases.setdefault(
                    (vid, identity(v)), f"applied-{item['id']}-{vid}"
                )
            else:
                mapping[vid] = vid
        for v in old["vertices"]:
            v["id"] = mapping[v["id"]]
        old["edges"] = [[mapping[a], mapping[b]] for a, b in old["edges"]]
        old["bindings"] = {
            "/".join(mapping[i] for i in key.split("/")): {
                side: {mapping[vid]: address for vid, address in values.items()}
                for side, values in binding.items()
            }
            for key, binding in old["bindings"].items()
        }
        old["address_book"] = {mapping[k]: v for k, v in old["address_book"].items()}
        item["active_graph"] = old
        item["active_path"] = [mapping[vid] for vid in item["active_path"]]
        for key in ("bindings", "address_book"):
            for k, v in old[key].items():
                display[key].setdefault(k, v)
        for v in old["vertices"]:
            if v["id"] not in vertices:
                v = dict(v, runtime_only=True)
                for other in sorted(vertices.values(), key=lambda n: n["y"]):
                    if (
                        abs(v["x"] - other["x"]) < 244
                        and v["y"] < other["y"] + height(other) + 24
                        and v["y"] + height(v) + 24 > other["y"]
                    ):
                        v["y"] = other["y"] + height(other) + 48
                display["vertices"].append(v)
                vertices[v["id"]] = v
        for e in old["edges"]:
            if e not in display["edges"]:
                display["edges"].append(e)
    state = next(
        (v["state"] for v in views if v["state"] == "error"),
        next(
            (v["state"] for v in views if v["state"] != "active"),
            "active" if views else "pending",
        ),
    )
    events = [
        dict(e)
        for e in c.execute(
            "SELECT ts,message FROM configuration_events WHERE configuration_id IN (SELECT id FROM configurations WHERE topology_id=?) ORDER BY id DESC LIMIT 30",
            (ident,),
        )
    ]
    cleanup = [
        dict(x)
        for x in c.execute(
            "SELECT * FROM retirements WHERE configuration_id IN (SELECT id FROM configurations WHERE topology_id=?)",
            (ident,),
        )
    ]
    cleanup_failed = any(
        database.get_node(c, x["node_id"])["state"] == "error" for x in cleanup
    )
    return dict(
        r,
        graph=graph,
        view_graph=display,
        configurations=views,
        state="deleting" if r["deleted"] else state,
        error=next((v["error"] for v in views if v["error"]), ""),
        events=events,
        cleanup=cleanup,
        sync=dict(
            retry=cleanup_failed or any(v["state"] in ("error", "waiting") for v in views),
            state=state,
        ),
        warnings=warnings(graph),
    )


def listing(c):
    return dict(
        resources(c),
        topologies=[detail(c, r["id"]) for r in c.execute(database.ordered("topologies"))],
    )


def delete(c, ident):
    get_topology(c, ident)
    c.execute("UPDATE topologies SET deleted=1,revision=revision+1 WHERE id=?", (ident,))
    c.execute(
        "UPDATE configurations SET desired=NULL,deleted=1,state='deleting' WHERE topology_id=?",
        (ident,),
    )
    if not members(c, ident):
        c.execute("DELETE FROM topologies WHERE id=?", (ident,))
        return dict(deleted=True)
    return dict(deleted=False)


def action(c, body, ident):
    row = get_topology(c, ident)
    if body.get("action") == "failover":
        enabled = body.get("enabled")
        if type(enabled) is not bool or body.get("revision") != row["revision"]:
            raise errors.UserError("拓扑已变化或开关无效，请刷新", status=409)
        c.execute("UPDATE topologies SET failover_enabled=?,revision=revision+1 WHERE id=?", (int(enabled), ident))
        c.execute("UPDATE configurations SET failover='{}' WHERE topology_id=?", (ident,))
        return dict(id=ident)
    if body.get("action") != "retry":
        raise errors.UserError("只支持重试同步")
    if body.get("revision") != row["revision"]:
        raise errors.UserError("拓扑已变化，请刷新", status=409)
    for config in members(c, ident):
        c.execute("UPDATE configurations SET failover='{}' WHERE id=?", (config["id"],))
        execution.retry(c, config["id"])
    return dict(id=ident)


def configs(c):
    result = []
    for r in sorted(
        [dict(r) for r in c.execute("SELECT * FROM configurations WHERE deleted=0")],
        key=order(c),
    ):
        v = view(c, r)
        parent = get_topology(c, r["topology_id"])
        plan = json.loads(r["applied"])["plan"] if r["applied"] else json.loads(r["desired"])
        target = database.get_target(c, plan["target"])
        result.append(
            dict(
                v,
                source="topology",
                topology_id=r["topology_id"],
                topology_name=parent["name"],
                group_id=parent["group_id"],
                entry_name=database.get_node(c, plan["entry"])["name"],
                entry_tags=node_tags.listing(c, plan["entry"]),
                target_name=target["name"],
            )
        )
    return dict(configs=result + external_configurations.listing(c), groups=[
        dict(g, source=source) for kind,source in (("topologies","topology"),("external_configurations","external"))
        for g in database.get_groups(c, kind)
    ])
