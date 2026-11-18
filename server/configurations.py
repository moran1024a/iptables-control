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
import node_visibility
import quotas
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


def order(c, account_id=None):
    ranks = {r["id"]: i for i, r in enumerate(database.owned_rows(c, "topologies", account_id))}
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


def preview(c, body, account_id=None):
    if body.get("id") is not None:
        database.require_owner(c, "topologies", body["id"], account_id)
    graph = topology.validate(c, body.get("graph"), complete=not body.get("layout_only"), account_id=account_id)
    visible = database.visible_address_nodes(c, account_id)
    if body.get("layout_only"):
        return dict(graph=node_visibility.graph(topology.arrange(graph), visible))
    prefix, required = body.get("prefix", []), body.get("required", [])
    if any(
        not isinstance(x, list) or any(not isinstance(i, str) for i in x)
        for x in (prefix, required)
    ):
        topology.fail("无效路径选择")
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
        graph=node_visibility.graph(topology.arrange(graph) if body.get("arrange") else graph, visible),
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


def entry_port(c, graph, vertex, port, prior, *, reassign=False):
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
    if reassign and port is not None and (port in used or not validation.in_ports(port, ranges)):
        port = None
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


def save_topology(c, body, ident=None, *, account_id=None):
    if ident is not None:
        database.require_owner(c, "topologies", ident, account_id)
    old = get_topology(c, ident) if ident is not None else None
    if old and old["deleted"]:
        raise errors.UserError("拓扑正在删除", status=409)
    if old and body.get("revision") != old["revision"]:
        raise errors.UserError("拓扑已变化，请刷新后重试", status=409)
    enabled = body.get("failover_enabled", bool(old["failover_enabled"]) if old else False)
    if type(enabled) is not bool:
        raise errors.UserError("自动灾备开关无效", "failover_enabled")
    if old and old["suspended"]:
        if body.get("switch_only") or body.get("resume") is not True:
            raise errors.UserError("请编辑并保存以恢复链路", status=409)
        if c.execute(
            "SELECT 1 FROM retirements r JOIN configurations f ON f.id=r.configuration_id WHERE f.topology_id=?",
            (ident,),
        ).fetchone():
            raise errors.UserError("等待节点确认清理旧规则后才能恢复链路", status=409)
    graph = topology.validate(c, body.get("graph"), account_id=account_id)
    existing = members(c, ident) if ident else []
    if body.get("switch_only"):
        if not old:
            topology.fail("请先保存拓扑")
        if topology.snapshot(graph) != topology.snapshot(topology.validate(c, json.loads(old["graph"]), account_id=account_id)):
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
        c, "topologies", body.get("group_id", old["group_id"] if old else None), account_id,
    )
    graph = topology.arrange(graph)
    limit = quotas.config_limit(c, account_id)
    before = quotas.config_count(c, account_id) if limit is not None else 0
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
    if old and old["suspended"]:
        c.execute("UPDATE configurations SET desired=NULL WHERE topology_id=?", (ident,))
        c.execute("UPDATE topologies SET suspended=0 WHERE id=?", (ident,))
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
        # After suspension another configuration may have taken the old port.
        port = entry_port(c, graph, pair[0], port, prior, reassign=bool(old and old["suspended"]))
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
                and not (old and old["suspended"])
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
    c.execute("UPDATE topologies SET graph=?,suspended=0 WHERE id=?", (execution.dump(graph), ident))
    if limit is not None:
        quotas.check_config_limit(limit, before, quotas.config_count(c, account_id))
    return dict(id=ident, configurations=len(chosen), warnings=warnings(graph))


def view(c, r, suspended=False, visible=None):
    applied = json.loads(r["applied"]) if r["applied"] else None
    desired = json.loads(r["desired"]) if r["desired"] else None
    job = c.execute("SELECT * FROM jobs WHERE configuration_id=?", (r["id"],)).fetchone()
    current = applied["plan"] if applied else None
    state = "suspended" if suspended else r["state"]
    error = r["error"]
    failover_status = json.loads(r["failover"])
    entry = (current or desired or {}).get("entry")
    entry_private = visible is not None and entry not in visible
    private_path = visible is not None and any(
        "node_id" in v and v["node_id"] not in visible
        for p in (current, desired) if p for v in p["graph"]["vertices"]
    )
    if private_path:
        if error and state != "quota":
            error = "链路同步存在异常，请联系管理员"
        if failover_status.get("phase") == "unavailable" and failover_status.get("message"):
            failover_status["message"] = "无可用备用路径"
    if state == "quota":
        if current:
            state = "quota_waiting"
            error += "，当前路径仍生效"
        elif c.execute("SELECT 1 FROM retirements WHERE configuration_id=?", (r["id"],)).fetchone():
            state = "quota_cleaning"
    if state == "active" and applied:
        states = [
            node_state.node_status(c, database.get_node(c, x["node_id"]), int(time.time()))
            for x in applied["rules"]
        ]
        if any(s in ("error", "offline", "skew") for s in states):
            state = "error"
            error = "路径节点异常或离线"
    plan = None if suspended else current or desired
    target = (
        execution.applied_target(c, r) if current else target_addresses.resolve(c, plan)
    ) if plan and not r["deleted"] else None
    connection = (
        target_config.connection(
            target, current["ingress_host"], current["port"], r["name"]
        )
        if current and not (r["deleted"] or suspended or entry_private)
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
        pending_path=desired["graph_path"] if desired and state not in ("active", "suspended", "quota", "quota_cleaning") else [],
        active_graph=node_visibility.graph(current["graph"], visible) if current else None,
        pending_graph=node_visibility.graph(desired["graph"], visible)
                      if desired and state not in ("active", "suspended", "quota", "quota_cleaning") else None,
        port=None if entry_private else (current or desired or {}).get("port"),
        connection=connection,
        target_address=target_addresses.describe(c, plan, target) if target else None,
        failover=failover_status,
    )


def resources(c, account_id=None):
    allowed = database.allowed_nodes(c, account_id)
    hidden = database.hidden_nodes(c, account_id)
    nodes = [n for n in c.execute(database.ordered("nodes")) if allowed is None or n["id"] in allowed]
    node_groups = {n["group_id"] for n in nodes}
    return dict(
        groups=database.get_groups(c, "topologies", account_id),
        node_groups=[g for g in database.get_groups(c, "nodes") if allowed is None or g["id"] in node_groups],
        target_groups=database.get_groups(c, "targets", account_id),
        nodes=[
            dict(
                id=n["id"],
                name=n["name"],
                addr="" if n["id"] in hidden else n["addr"],
                addresses_hidden=n["id"] in hidden,
                weight=n["weight"],
                group_id=n["group_id"],
                tags=node_tags.listing(c, n["id"]),
                addresses=node_visibility.address_labels(node_addresses.listing(c, n))
                          if n["id"] in hidden else node_addresses.listing(c, n),
                network=node_addresses.network(c, n["id"]) if allowed is None
                        else dict(addresses=[], sampled_at=None, complete=False),
                mode=n["desired_mode"],
                status=node_state.node_status(c, n, int(time.time())),
            )
            for n in nodes
        ],
        targets=[dict(r, addresses=target_addresses.listing(r)) for r in database.owned_rows(c, "targets", account_id)],
    )


def detail(c, ident, account_id=None):
    database.require_owner(c, "topologies", ident, account_id)
    r = get_topology(c, ident)
    configs = members(c, ident)
    visible = database.visible_address_nodes(c, account_id)
    views = [view(c, x, r["suspended"], visible) for x in configs]
    graph = node_visibility.graph(json.loads(r["graph"]), visible)
    allowed = database.allowed_nodes(c, account_id)
    weights = {n["id"]: n["weight"] for n in c.execute("SELECT id,weight FROM nodes")
               if allowed is None or n["id"] in allowed}
    for vertex in graph["vertices"]:
        if vertex.get("node_id") in weights:
            vertex["weight"] = weights[vertex["node_id"]]
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
    if visible is not None:
        for item in cleanup:
            if item["node_id"] not in visible:
                item["port"] = None
        for item in events:
            if item["message"].startswith("自动灾备："):
                item["message"] = "自动灾备状态更新"
    if r["suspended"]:
        state = "suspended"
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
            retry=(account_id is None or not r["suspended"])
                  and (cleanup_failed or any(v["state"] in ("error", "waiting") for v in views)),
            state=state,
        ),
        warnings=warnings(graph),
    )


def listing(c, account_id=None):
    return dict(
        resources(c, account_id),
        topologies=[detail(c, r["id"], account_id) for r in database.owned_rows(c, "topologies", account_id)],
    )


def delete(c, ident, account_id=None):
    database.require_owner(c, "topologies", ident, account_id)
    c.execute("UPDATE topologies SET deleted=1,revision=revision+1 WHERE id=?", (ident,))
    c.execute(
        "UPDATE configurations SET desired=NULL,deleted=1,state='deleting' WHERE topology_id=?",
        (ident,),
    )
    if not members(c, ident):
        c.execute("DELETE FROM topologies WHERE id=?", (ident,))
        return dict(deleted=True)
    return dict(deleted=False)


def action(c, body, ident, account_id=None):
    database.require_owner(c, "topologies", ident, account_id)
    row = get_topology(c, ident)
    if row["suspended"]:
        raise errors.UserError("链路已停用，请重新编辑并保存", status=409)
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


def configs(c, account_id=None):
    result = []
    allowed = database.allowed_nodes(c, account_id)
    visible = database.visible_address_nodes(c, account_id)
    for r in sorted(
        [dict(r) for r in c.execute(
            "SELECT f.* FROM configurations f JOIN topologies t ON t.id=f.topology_id "
            "JOIN item_groups g ON g.id=t.group_id WHERE f.deleted=0 AND g.account_id IS ?", (account_id,)
        )],
        key=order(c, account_id),
    ):
        parent = get_topology(c, r["topology_id"])
        v = view(c, r, parent["suspended"], visible)
        plan = json.loads(r["applied"])["plan"] if r["applied"] else json.loads(r["desired"])
        target = database.get_target(c, plan["target"])
        entry_visible = allowed is None or plan["entry"] in allowed
        result.append(
            dict(
                v,
                source="topology",
                topology_id=r["topology_id"],
                topology_name=parent["name"],
                group_id=parent["group_id"],
                entry_name=database.get_node(c, plan["entry"])["name"] if entry_visible else "已撤权节点",
                entry_tags=node_tags.listing(c, plan["entry"]) if entry_visible else [],
                target_name=target["name"],
            )
        )
    items = result + external_configurations.listing(c, account_id)
    result = dict(configs=items, groups=[
        dict(g, source=source) for kind,source in (("topologies","topology"),("external_configurations","external"))
        for g in database.get_groups(c, kind, account_id)
    ])
    if account_id is not None:
        result["quota"] = dict(used=len(items), limit=quotas.config_limit(c, account_id))
    return result
