"""The one canvas model. Structural validation never consults runtime readiness."""

import copy
import math
import json
import re
from collections import Counter
import database
import errors
import node_addresses
import target_addresses
import validation

MAX_VERTICES = 256
MAX_EDGES = MAX_VERTICES * (MAX_VERTICES - 1) // 2
MAX_COORD = 1000000


def fail(message, field="graph"):
    raise errors.UserError(message, field)


def endpoints(graph):
    return tuple(
        next((v["id"] for v in graph["vertices"] if v["kind"] == k), None)
        for k in ("entry", "target")
    )


def snapshot(graph):
    if not graph:
        return None
    out = copy.deepcopy(graph)
    out.pop("address_book", None)
    for v in out["vertices"]:
        v.pop("x", None)
        v.pop("y", None)
        v.pop("layer", None)
        if v.get("node_id"):
            v.pop("weight", None)
    return out


def validate(c, raw, complete=True):
    if not isinstance(raw, dict) or raw.get("version") != 2 or raw.get("routing_version") != 2:
        fail("请使用新版节点拓扑")
    vertices, edges = raw.get("vertices"), raw.get("edges")
    if not isinstance(vertices, list) or len(vertices) > MAX_VERTICES:
        fail("画布最多包含 256 个节点")
    if not isinstance(edges, list) or len(edges) > MAX_EDGES:
        fail("连线过多")
    graph = dict(
        version=2,
        routing_version=2,
        vertices=[],
        edges=[],
        bindings={},
        address_book={},
        ports={},
    )
    ids = set()
    for raw_v in vertices:
        if not isinstance(raw_v, dict):
            fail("无效节点")
        ident, kind = raw_v.get("id"), raw_v.get("kind")
        if (
            not isinstance(ident, str)
            or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", ident)
            or ident in ids
        ):
            fail("节点标识无效或重复")
        if kind not in ("entry", "node", "target", "direct"):
            fail("画布只支持入口、节点、direct 和目标")
        ids.add(ident)
        v = dict(id=ident, kind=kind, disabled=False)
        for axis in ("x", "y"):
            value = raw_v.get(axis, 40)
            if (
                type(value) not in (int, float)
                or not math.isfinite(value)
                or abs(value) > MAX_COORD
            ):
                fail("画布坐标无效")
            v[axis] = value
        if kind == "target":
            target = database.get_target(c, raw_v.get("target_id"))
            v["target_id"] = target["id"]
            graph["address_book"][ident] = {a["id"]: a for a in target_addresses.listing(target)}
        elif kind == "direct":
            v["weight"] = validation.check_weight(raw_v.get("weight", 0), "vertex:" + ident)
        else:
            n = database.get_node(c, raw_v.get("node_id"))
            if n["deleting"]:
                fail(f"节点「{n['name']}」正在删除", "vertex:" + ident)
            v["node_id"] = n["id"]
            v["weight"] = n["weight"]
            book = {a["id"]: dict(a) for a in node_addresses.listing(c, n)}
            graph["address_book"][ident] = book
            for side in ("in_address", "out_address"):
                value = raw_v.get(side)
                if isinstance(value, str) and (
                    value in book or side == "out_address" and value == node_addresses.AUTO
                ):
                    v[side] = value
            if kind == "entry":
                address = raw_v.get("address") or (next(iter(book)) if len(book) == 1 else None)
                if complete and (not isinstance(address, str) or address not in book):
                    fail(f"请选择「{n['name']}」的接收 IP", "vertex:" + ident)
                v["address"] = address
        graph["vertices"].append(v)
    by_id = {v["id"]: v for v in graph["vertices"]}
    seen = set()
    raw_bindings = raw.get("bindings", {})
    if not isinstance(raw_bindings, dict):
        fail("无效的 IP 选择")
    for edge in edges:
        if (
            not isinstance(edge, list)
            or len(edge) != 2
            or any(not isinstance(x, str) or x not in ids for x in edge)
        ):
            fail("连线引用不存在的节点")
        a, b = edge
        if a == b or (a, b) in seen:
            fail("不能自连或重复连线", "vertex:" + a)
        if by_id[a]["kind"] == "target" or by_id[b]["kind"] == "entry":
            fail("入口不能接收入站连线，目标不能继续转发", "vertex:" + a)
        if by_id[a].get("node_id") and by_id[a].get("node_id") == by_id[b].get("node_id"):
            fail("不能将同一台机器连接到自身", "vertex:" + b)
        seen.add((a, b))
        graph["edges"].append([a, b])
        key = a + "/" + b
        source = raw_bindings.get(key, {})
        if not isinstance(source, dict):
            fail("无效的连线地址")
        mapping = {"from": {}, "to": {}}
        for side, vid in (("from", a), ("to", b)):
            if by_id[vid]["kind"] == "direct":
                continue
            selected = source.get(side, {})
            if not isinstance(selected, dict):
                fail("无效的连线地址")
            book = graph["address_book"][vid]
            address = selected.get(vid)
            if address is None:
                address = (
                    node_addresses.AUTO
                    if side == "from"
                    else "main" if by_id[vid]["kind"] == "target"
                    else next(iter(book)) if len(book) == 1 else None
                )
            if (not isinstance(address, str) or address not in book) and not (
                side == "from" and address == node_addresses.AUTO
            ):
                if complete:
                    fail(
                        "请为连线选择" + ("发出" if side == "from" else "接收") + " IP",
                        "vertex:" + vid,
                    )
            mapping[side][vid] = address
        graph["bindings"][key] = mapping
    for v in graph["vertices"]:
        if v["kind"] != "direct":
            continue
        incoming = [a for a, b in graph["edges"] if b == v["id"]]
        outgoing = [b for a, b in graph["edges"] if a == v["id"]]
        if len(incoming) > 1 or len(outgoing) > 1:
            fail("direct 只能连接一个上游和一个下游", "vertex:" + v["id"])
        if any(by_id[x]["kind"] == "direct" for x in incoming + outgoing):
            fail("不能连续连接 direct", "vertex:" + v["id"])
        if incoming and outgoing and by_id[incoming[0]].get("node_id") == by_id[outgoing[0]].get("node_id"):
            fail("不能通过 direct 连接同一台机器", "vertex:" + v["id"])
        if complete and (not incoming or not outgoing):
            fail("请完成 direct 两端的连接", "vertex:" + v["id"])
    indegree = dict.fromkeys(ids, 0)
    adj = {i: [] for i in ids}
    for a, b in graph["edges"]:
        indegree[b] += 1
        adj[a].append(b)
    queue = [i for i in ids if not indegree[i]]
    for i in queue:
        for b in adj[i]:
            indegree[b] -= 1
            if indegree[b] == 0:
                queue.append(b)
    if len(queue) != len(ids):
        fail("拓扑存在循环连接")
    if complete:
        for kind, name in (("entry", "入口"), ("target", "目标")):
            if not any(v["kind"] == kind for v in graph["vertices"]):
                fail("请至少添加一个" + name)
    ports = raw.get("ports", {})
    if not isinstance(ports, dict):
        fail("无效的端口列表")
    graph["ports"] = {
        k: validation.check_port(v, "port")
        for k, v in ports.items()
        if isinstance(k, str) and len(k.split("/")) == 2 and all(i in ids for i in k.split("/"))
    }
    if complete and len(pairs(graph)) > 64:
        fail("每张拓扑最多生成 64 份可达配置")
    return graph


def topological_order(graph):
    adj = {v["id"]: [] for v in graph["vertices"]}
    degree = dict.fromkeys(adj, 0)
    for a, b in graph["edges"]:
        adj[a].append(b)
        degree[b] += 1
    order = [i for i in adj if not degree[i]]
    for i in order:
        for b in adj[i]:
            degree[b] -= 1
            if degree[b] == 0:
                order.append(b)
    if len(order) != len(adj):
        fail("拓扑存在循环连接")
    return order, adj


def path_weight(graph, path):
    by = {v["id"]: v for v in graph["vertices"]}
    return sum(by[i].get("weight", 0) for i in path if by[i]["kind"] in ("node", "direct"))


def find_path(graph, *, entry=None, target=None, required=(), prefix=(), preferred=(),
              allowed=None, edge_allowed=None, forbidden=()):
    entry0, target0 = endpoints(graph)
    entry, target = entry or entry0, target or target0
    by = {v["id"]: v for v in graph["vertices"]}
    if entry not in by or target not in by or not set(required) <= by.keys():
        return None
    order, adj = topological_order(graph)
    node_ok, edge_ok = {}, {}

    def available(i):
        if i not in node_ok:
            node_ok[i] = not allowed or allowed(by[i])
        return node_ok[i]

    def connected(a, b):
        if (a, b) not in edge_ok:
            edge_ok[a, b] = not edge_allowed or edge_allowed(a, b)
        return edge_ok[a, b]

    def valid(path):
        physical = [by[i]["node_id"] for i in path if by[i].get("node_id")]
        return (len(physical) == len(set(physical)) and len(physical) < 64
                and all(available(i) for i in path)
                and all(connected(a, b) for a, b in zip(path, path[1:])))

    forbidden = [list(p) for p in forbidden]
    if (preferred and all(i in by for i in preferred) and preferred[0] == entry
            and preferred[-1] == target and list(preferred[:len(prefix)]) == list(prefix)
            and set(required) <= set(preferred)
            and all(b in adj[a] for a, b in zip(preferred, preferred[1:]))
            and list(preferred) not in forbidden and valid(preferred)):
        return list(preferred)
    start = list(prefix) if prefix else [entry]
    if (start[0] != entry or any(i not in by for i in start)
            or any(b not in adj[a] for a, b in zip(start, start[1:])) or not valid(start)):
        return None
    reverse = {i: [] for i in by}
    for a, bs in adj.items():
        for b in bs:
            reverse[b].append(a)
    reachable, todo = {target}, [target]
    for b in todo:
        for a in reverse[b]:
            if a not in reachable:
                reachable.add(a)
                todo.append(a)
    counts = Counter(v["node_id"] for v in by.values() if v.get("node_id"))
    repeated = {n for n, count in counts.items() if count > 1}
    required = set(required)

    def state(path):
        return (frozenset(by[i]["node_id"] for i in path if by[i].get("node_id") in repeated),
                frozenset(required.intersection(path)),
                tuple(j for j, p in enumerate(forbidden) if p[:len(path)] == path),
                sum(by[i]["kind"] != "direct" for i in path))

    # Maximum-weight paths in topological order. Extra state preserves the ban on
    # repeated physical machines, required waypoints and previously failed paths.
    best = {i: {} for i in by}
    best[start[-1]][state(start)] = (path_weight(graph, start), start)
    visits = 0
    for a in order:
        for score, path in list(best[a].values()):
            visits += 1
            if visits > 32768:
                fail("路径组合过多，请简化重复节点")
            if a == target:
                continue
            for b in adj[a]:
                candidate = path + [b]
                if b not in reachable or not valid(candidate):
                    continue
                value = score + (by[b].get("weight", 0) if by[b]["kind"] in ("node", "direct") else 0)
                key = state(candidate)
                old = best[b].get(key)
                tie = (old is not None and value == old[0]
                       and (len(candidate), candidate) < (len(old[1]), old[1]))
                if old is None or value > old[0] or tie:
                    best[b][key] = value, candidate
    options = [(score, path) for score, path in best[target].values()
               if required <= set(path) and path not in forbidden]
    if not options:
        return None
    score = max(x[0] for x in options)
    options = [p for v, p in options if v == score]
    return min(options, key=lambda p: (len(p), p))


def wire_graph(graph, path):
    """Fold visual edge modifiers into real links, preserving endpoint IP choices."""
    by = {v["id"]: v for v in graph["vertices"]}
    real = [i for i in path if by[i]["kind"] != "direct"]
    out = dict(graph, vertices=[by[i] for i in real], edges=[], bindings={})
    for a, b in zip(real, real[1:]):
        start, end = path.index(a), path.index(b)
        first, last = path[start + 1], path[end - 1]
        out["edges"].append([a, b])
        out["bindings"][a + "/" + b] = {
            "from": graph["bindings"][a + "/" + first]["from"],
            "to": graph["bindings"][last + "/" + b]["to"],
        }
    return out, real


def pairs(graph):
    return {
        (a["id"], b["id"]): path
        for a in graph["vertices"]
        if a["kind"] == "entry"
        for b in graph["vertices"]
        if b["kind"] == "target"
        if (path := find_path(graph, entry=a["id"], target=b["id"]))
    }


def check_path(graph, path):
    if not isinstance(path, list) or len(path) < 2 or any(not isinstance(i, str) for i in path):
        fail("请选择完整路径", "path")
    vertices = {v["id"]: v for v in graph["vertices"]}
    if (
        vertices.get(path[0], {}).get("kind") != "entry"
        or vertices.get(path[-1], {}).get("kind") != "target"
        or find_path(graph, entry=path[0], target=path[-1], prefix=path) != path
    ):
        fail("所选路径无法到达目标，或重复经过同一节点", "path")
    return path


def compile_plan(graph, path, port=None):
    check_path(graph, path)
    vertices = {v["id"]: v for v in graph["vertices"]}
    edges = [list(e) for e in zip(path, path[1:])]
    keys = {a + "/" + b for a, b in edges}
    small = dict(
        version=2,
        routing_version=2,
        vertices=[copy.deepcopy(vertices[i]) for i in path],
        edges=edges,
        bindings={k: copy.deepcopy(v) for k, v in graph["bindings"].items() if k in keys},
        address_book={
            k: copy.deepcopy(v) for k, v in graph["address_book"].items() if k in path
        },
        ports={},
    )
    return dict(
        entry=vertices[path[0]]["node_id"],
        target=vertices[path[-1]]["target_id"],
        port=port,
        graph=small,
        graph_path=path[:],
    )


def documents(c, include_pending=True, live_only=False):
    # live_only skips topologies and configurations that are already being deleted.
    live = " WHERE deleted=0" if live_only else ""
    if include_pending:
        for r in c.execute("SELECT name,graph FROM topologies" + live):
            yield r["name"], dict(graph=json.loads(r["graph"]))
    for r in c.execute("SELECT name,desired,applied FROM configurations" + live):
        for k in (("desired", "applied") if include_pending else ("applied",)):
            if r[k]:
                d = json.loads(r[k])
                yield r["name"], d.get("plan", d)
    for r in c.execute("SELECT payload FROM jobs"):
        d = json.loads(r[0])
        yield "执行中的配置", d["plan"]
        if live_only:
            # Rules being replaced still need this node until the job finishes.
            yield "执行中的配置", dict(
                graph=dict(vertices=[dict(node_id=x["node_id"]) for x in d["new"] + d["old"]])
            )


def references(c, nid, live_only=False):
    return list(
        dict.fromkeys(
            name
            for name, d in documents(c, live_only=live_only)
            if any(v.get("node_id") == nid for v in d.get("graph", {}).get("vertices", []))
        )
    )


def arrange(graph):
    """Rooted DAG layers; modifiers occupy wires, not forwarding columns."""
    graph = copy.deepcopy(graph)
    by = {v["id"]: v for v in graph["vertices"]}
    order, all_adj = topological_order(graph)
    real = [i for i in order if by[i]["kind"] != "direct"]
    adj, parents = {i: [] for i in real}, {i: [] for i in real}
    for a in real:
        for b in all_adj[a]:
            targets = all_adj[b] if by[b]["kind"] == "direct" else [b]
            for target in targets:
                adj[a].append(target)
                parents[target].append(a)
    ranks = {i: 0 for i in real if by[i]["kind"] == "entry"}
    for a in real:
        if a in ranks:
            for b in adj[a]:
                ranks[b] = max(ranks.get(b, 0), ranks[a] + 1)
    terminal = max(ranks.values(), default=1)
    for i in list(ranks):
        if by[i]["kind"] == "target":
            ranks[i] = terminal
    layers = {}
    for i in real:
        by[i]["layer"] = ranks.get(i)
        if i in ranks:
            layers.setdefault(ranks[i], []).append(i)
    heights = {i: 62 + 24 * (max(1, len(graph.get("address_book", {}).get(i, {})))
                                + bool(by[i].get("node_id"))) for i in real}
    original = {i: by[i].get("y", 0) for i in real}
    for layer in layers.values():
        layer.sort(key=lambda i: (original[i], i))
    # Repeated neighbour-median sweeps reduce crossings while retaining stable ties.
    def positions():
        return {i: (j + .5) / len(layer) for layer in layers.values() for j, i in enumerate(layer)}
    for _ in range(4):
        for forward in (True, False):
            for rank in sorted(layers, reverse=not forward):
                if rank == 0:
                    continue
                pos = positions()
                neighbours = parents if forward else adj
                old = {i: j for j, i in enumerate(layers[rank])}
                def score(i):
                    values = sorted(pos[p] for p in neighbours[i] if p in pos)
                    return (values[len(values)//2] if values else pos[i], old[i])
                layers[rank].sort(key=score)
    centers = {}
    if layers:
        anchor = max(layers, key=lambda r: sum(heights[i]+48 for i in layers[r]))
        y = 60
        for i in layers[anchor]:
            centers[i] = y + heights[i]/2
            y += heights[i] + 48
        axis = sum(centers.values()) / len(centers)
        def align(rank, neighbours):
            desired = {}
            for i in layers[rank]:
                values = sorted(centers[p] for p in neighbours[i] if p in centers)
                desired[i] = (values[(len(values)-1)//2]+values[len(values)//2])/2 if values else axis
            bottom = -math.inf
            for i in layers[rank]:
                centers[i] = max(desired[i], bottom+48+heights[i]/2)
                bottom = centers[i]+heights[i]/2
            shift = sum(centers[i]-desired[i] for i in layers[rank])/len(layers[rank])
            for i in layers[rank]:
                centers[i] -= shift
        for rank in sorted(r for r in layers if r > anchor):
            align(rank, parents)
        for rank in sorted((r for r in layers if r < anchor), reverse=True):
            align(rank, adj)
        shift = 60-min(centers[i]-heights[i]/2 for i in ranks)
        for i, rank in ranks.items():
            by[i].update(x=40+rank*400, y=round(centers[i]+shift-heights[i]/2))
    bottom = max((by[i]["y"]+heights[i] for i in ranks), default=0)+130
    for j, i in enumerate(i for i in real if i not in ranks):
        by[i].update(x=40+(j%4)*400, y=bottom+(j//4)*180)
    placed = [(by[i]["x"], by[i]["y"], 220, heights[i]) for i in real]
    for i in order:
        v = by[i]
        if v["kind"] != "direct":
            continue
        incoming = [a for a, bs in all_adj.items() if i in bs]
        outgoing = all_adj[i]
        if incoming and outgoing:
            a, b = by[incoming[0]], by[outgoing[0]]
            x = (a["x"]+220+b["x"]-110)/2
            y = (a["y"]+heights.get(a["id"],60)/2+b["y"]+heights.get(b["id"],60)/2)/2-28
        else:
            x, y = 40, bottom+180
        while any(x < px+w+12 and x+110 > px-12 and y < py+h+12 and y+56 > py-12 for px,py,w,h in placed):
            y += 76
        v.update(x=round(x), y=round(y), layer=None)
        placed.append((v["x"],v["y"],110,56))
    return graph


def compile_routing(graph, path):
    graph, path = wire_graph(copy.deepcopy(graph), path)
    vertices = {v["id"]: v for v in graph["vertices"]}
    book = graph["address_book"]
    routes = []
    incoming = []

    def chosen(a, b, vid, side):
        key = node_addresses.edge_key(a, b)
        ident = graph["bindings"].get(key, {}).get(side, {}).get(vid)
        if (
            graph.get("routing_version") == 2
            and side == "from"
            and ident == node_addresses.AUTO
        ):
            return None
        try:
            return book[vid][ident]
        except KeyError:
            fail("路径缺少进出地址映射", "vertex:" + vid)

    for index, vid in enumerate(path):
        vertex = vertices[vid]
        if not vertex.get("node_id"):
            continue
        if vertex["kind"] == "entry":
            try:
                inc = book[vid][vertex["address"]]
            except KeyError:
                fail("请选择入口接收地址", "vertex:" + vid)
        else:
            inc = chosen(path[index - 1], vid, vid, "to")
        out = chosen(vid, path[index + 1], vid, "from")
        routes.append(node_addresses.route_token(inc, out, graph.get("routing_version", 1)))
        incoming.append(inc["host"])
    return dict(
        routing=routes, hop_addresses=incoming, ingress_host=incoming[0] if incoming else None
    )
