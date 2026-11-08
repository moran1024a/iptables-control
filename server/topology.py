"""The one canvas model. Structural validation never consults runtime readiness."""

import copy
import math
import json
import re
from collections import Counter, deque
import database
import errors
import node_addresses
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


def connections(graph):
    return [tuple(e) for e in graph["edges"]]


def snapshot(graph):
    if not graph:
        return None
    out = copy.deepcopy(graph)
    out.pop("address_book", None)
    for v in out["vertices"]:
        v.pop("x", None)
        v.pop("y", None)
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
        if kind not in ("entry", "node", "target"):
            fail("画布只支持入口、节点和目标")
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
            v["target_id"] = database.get_target(c, raw_v.get("target_id"))["id"]
        else:
            n = database.get_node(c, raw_v.get("node_id"))
            if n["deleting"]:
                fail(f"节点「{n['name']}」正在删除", "vertex:" + ident)
            v["node_id"] = n["id"]
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
            if not by_id[vid].get("node_id"):
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


def find_path(
    graph,
    *,
    entry=None,
    target=None,
    required=(),
    prefix=(),
    preferred=(),
    allowed=None,
    **unused,
):
    default_entry, default_target = endpoints(graph)
    entry = entry or default_entry
    target = target or default_target
    vertices = {v["id"]: v for v in graph["vertices"]}
    adj = {i: [] for i in vertices}
    if entry not in vertices or target not in vertices or not set(required) <= vertices.keys():
        return None
    for a, b in connections(graph):
        adj[a].append(b)

    def valid(path):
        physical = [vertices[i]["node_id"] for i in path if vertices[i].get("node_id")]
        return len(physical) == len(set(physical)) and all(
            not allowed or allowed(vertices[i]) for i in path
        )

    if (
        preferred
        and all(i in vertices for i in preferred)
        and preferred[0] == entry
        and preferred[-1] == target
        and list(preferred[: len(prefix)]) == list(prefix)
        and set(required) <= set(preferred)
        and all(b in adj[a] for a, b in zip(preferred, preferred[1:]))
        and valid(preferred)
    ):
        return list(preferred)
    reverse = {i: [] for i in vertices}
    for a, bs in adj.items():
        for b in bs:
            reverse[b].append(a)
    back = {target}
    queue = [target]
    for i in queue:
        for a in reverse[i]:
            if a not in back:
                back.add(a)
                queue.append(a)
    start = list(prefix) if prefix else [entry]
    if (
        start[0] != entry
        or any(i not in vertices for i in start)
        or any(b not in adj[a] for a, b in zip(start, start[1:]))
        or not valid(start)
    ):
        return None
    counts = Counter(v["node_id"] for v in vertices.values() if v.get("node_id"))
    repeated = {nid for nid, count in counts.items() if count > 1}
    required = set(required)

    def state(path):
        # The graph is acyclic. Only repeated physical machines and requested
        # waypoints can make two arrivals at the same vertex meaningfully different.
        return (
            path[-1],
            frozenset(
                vertices[i]["node_id"] for i in path if vertices[i].get("node_id") in repeated
            ),
            frozenset(required.intersection(path)),
        )

    paths = deque([start])
    seen = {state(start)}
    visits = 0
    while paths:
        path = paths.popleft()
        i = path[-1]
        visits += 1
        if visits > 32768:
            fail("路径组合过多，请简化重复节点")
        if i not in back or len(path) > 64 or not valid(path):
            continue
        if i == target:
            if required <= set(path):
                return path
            continue
        for b in sorted(adj[i]):
            if b not in path:
                candidate = path + [b]
                if b not in back or not valid(candidate):
                    continue
                key = state(candidate)
                if key not in seen:
                    seen.add(key)
                    paths.append(candidate)
    return None


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


def documents(c, include_pending=True):
    if include_pending:
        for r in c.execute("SELECT name,graph FROM topologies"):
            yield r["name"], dict(graph=json.loads(r["graph"]))
    for r in c.execute("SELECT name,desired,applied FROM configurations"):
        for k in (("desired", "applied") if include_pending else ("applied",)):
            if r[k]:
                d = json.loads(r[k])
                yield r["name"], d.get("plan", d)
    for r in c.execute("SELECT payload FROM jobs"):
        d = json.loads(r[0])
        yield "执行中的配置", d["plan"]


def references(c, nid):
    return list(
        dict.fromkeys(
            name
            for name, d in documents(c)
            if any(v.get("node_id") == nid for v in d.get("graph", {}).get("vertices", []))
        )
    )


def arrange(graph):
    """Lay out nodes by dependency, aligning connected node centres."""
    graph = copy.deepcopy(graph)
    nodes = {v["id"]: v for v in graph["vertices"]}
    edges = connections(graph)
    graph["edges"] = [list(e) for e in edges]
    incoming = {i: [] for i in nodes}
    outgoing = {i: [] for i in nodes}
    for a, b in edges:
        incoming[b].append(a)
        outgoing[a].append(b)
    degree = {i: len(incoming[i]) for i in nodes}
    queue = [i for i in nodes if not degree[i]]
    ranks = dict.fromkeys(nodes, 0)
    for i in queue:
        for b in outgoing[i]:
            ranks[b] = max(ranks[b], ranks[i] + 1)
            degree[b] -= 1
            if not degree[b]:
                queue.append(b)
    if len(queue) != len(nodes):
        fail("拓扑存在循环连接")
    final_rank = max(ranks.values(), default=0)
    for v in graph["vertices"]:
        if v["kind"] == "target":
            ranks[v["id"]] = final_rank
    layers = {}
    for i in queue:
        layers.setdefault(ranks[i], []).append(i)
    card_height = lambda v: 62 + 24 * (
        max(1, len(graph.get("address_book", {}).get(v["id"], {})))
        + (1 if graph.get("routing_version") == 2 and v.get("node_id") else 0)
    )
    heights = {i: card_height(v) for i, v in nodes.items()}
    if not layers:
        return graph
    original_y = {i: v.get("y", 0) for i, v in nodes.items()}
    for layer in layers.values():
        layer.sort(key=lambda i: original_y[i])
    # The tallest column establishes branch lanes. Neighbouring columns follow
    # connected component centres, not member positions or the whole column's top.
    anchor = max(
        layers,
        key=lambda rank: sum(heights[i] for i in layers[rank]) + 48 * (len(layers[rank]) - 1),
    )
    centers = {}
    y = 60
    for i in layers[anchor]:
        centers[i] = y + heights[i] / 2
        y += heights[i] + 48
    axis = centers[layers[anchor][0]]

    def align(rank, neighbours):
        layer = layers[rank]
        desired = {}
        for i in layer:
            adjacent = [p for p in neighbours[i] if p in centers]
            if adjacent:
                distance = min(abs(rank - ranks[p]) for p in adjacent)
                positions = sorted(
                    centers[p] for p in adjacent if abs(rank - ranks[p]) == distance
                )
                desired[i] = (
                    positions[(len(positions) - 1) // 2] + positions[len(positions) // 2]
                ) / 2
            else:
                desired[i] = axis
        layer.sort(key=lambda i: (desired[i], original_y[i]))
        bottom = -math.inf
        for i in layer:
            centers[i] = max(desired[i], bottom + 48 + heights[i] / 2)
            bottom = centers[i] + heights[i] / 2
        # Keep colliding siblings centred on their neighbours instead of
        # accumulating a downward offset at each column.
        shift = sum(centers[i] - desired[i] for i in layer) / len(layer)
        for i in layer:
            centers[i] -= shift

    for rank in sorted(r for r in layers if r > anchor):
        align(rank, incoming)
    for rank in sorted((r for r in layers if r < anchor), reverse=True):
        align(rank, outgoing)
    shift = 60 - min(centers[i] - heights[i] / 2 for i in nodes)
    x = 40
    for rank, layer in sorted(layers.items()):
        for i in layer:
            y = centers[i] + shift - heights[i] / 2
            nodes[i].update(x=x, y=round(y))
        x += 380
    return graph


def compile_routing(graph, path):
    graph = copy.deepcopy(graph)
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
