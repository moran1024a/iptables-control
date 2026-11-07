"""Canvas topology, bounded path selection and adapters to the rule transaction engine."""
import copy
import hashlib
import json
import math
import random
import re
import time

import database
import errors
import validation

MAX_VERTICES = 256
MAX_EDGES = 4096
MAX_VISITS = 8192
MAX_COORD = 1000000


def fail(message, field="graph"):
    raise errors.UserError(message, field)


def endpoints(graph):
    return tuple(next((v["id"] for v in graph["vertices"] if v["kind"] == k), None)
                 for k in ("entry", "target"))


def connections(graph):
    """Pool boundaries are endpoints; accept member endpoints from earlier drafts."""
    owners = {v["id"]: v.get("group") or v["id"] for v in graph["vertices"]}
    return list(dict.fromkeys((owners.get(a, a), owners.get(b, b)) for a, b in graph["edges"]))


def expanded_edges(graph):
    members = {g["id"]: [] for g in graph["groups"]}
    for v in graph["vertices"]:
        members.setdefault(v.get("group") or v["id"], []).append(v["id"])
    return [(a, b) for source, target in connections(graph)
            for a in members[source] for b in members[target]]


def arrange(graph):
    """Lay out outer components by dependency, keeping each pool's members together."""
    graph = copy.deepcopy(graph)
    groups = {g["id"]: [] for g in graph["groups"]}
    for v in graph["vertices"]:
        groups.setdefault(v.get("group") or v["id"], []).append(v)
    edges = connections(graph)
    graph["edges"] = [list(e) for e in edges]
    incoming = {i: [] for i in groups}
    outgoing = {i: [] for i in groups}
    for a, b in edges:
        incoming[b].append(a); outgoing[a].append(b)
    degree = {i: len(incoming[i]) for i in groups}
    queue = [i for i in groups if not degree[i]]
    ranks = dict.fromkeys(groups, 0)
    for i in queue:
        for b in outgoing[i]:
            ranks[b] = max(ranks[b], ranks[i] + 1)
            degree[b] -= 1
            if not degree[b]: queue.append(b)
    if len(queue) != len(groups): fail("拓扑存在循环连接")
    layers = {}
    for i in queue: layers.setdefault(ranks[i], []).append(i)
    pool_ids = {g['id'] for g in graph['groups']}
    heights = {i: max(144, 72 * len(vs) + 60) if i in pool_ids else 60
               for i, vs in groups.items()}
    if not layers: return graph
    original_y = {i: min((v['y'] for v in vs), default=0) for i, vs in groups.items()}
    for layer in layers.values(): layer.sort(key=lambda i: original_y[i])
    # The tallest column establishes branch lanes. Neighbouring columns follow
    # connected component centres, not member positions or the whole column's top.
    anchor = max(layers, key=lambda rank: sum(heights[i] for i in layers[rank]) + 48 * (len(layers[rank]) - 1))
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
                positions = sorted(centers[p] for p in adjacent if abs(rank - ranks[p]) == distance)
                desired[i] = positions[(len(positions) - 1) // 2]
            else: desired[i] = axis
        layer.sort(key=lambda i: (desired[i], original_y[i]))
        bottom = -math.inf
        for i in layer:
            centers[i] = max(desired[i], bottom + 48 + heights[i] / 2)
            bottom = centers[i] + heights[i] / 2

    for rank in sorted(r for r in layers if r > anchor): align(rank, incoming)
    for rank in sorted((r for r in layers if r < anchor), reverse=True): align(rank, outgoing)
    shift = 60 - min(centers[i] - heights[i] / 2 for i in groups)
    x = 40
    for rank, layer in sorted(layers.items()):
        width = 240 if any(i in pool_ids for i in layer) else 180
        for i in layer:
            y = centers[i] + shift - heights[i] / 2
            pool = i in pool_ids
            if pool:
                container = next(g for g in graph['groups'] if g['id'] == i)
                container.update(x=x, y=round(y), width=240, height=heights[i])
            for offset, v in enumerate(sorted(groups[i], key=lambda v: (v['y'], v['x']))):
                v['x'] = x + (width - 180) / 2
                v['y'] = round(y + (44 + offset * 72 if pool else 0))
        x += width + 120
    return graph


def validate(c, raw, complete=True):
    if not isinstance(raw, dict) or type(raw.get("version")) is not int or raw["version"] != 1:
        fail("无效的拓扑版本")
    vertices, groups, edges = (raw.get(k) for k in ("vertices", "groups", "edges"))
    if not isinstance(vertices, list) or len(vertices) > MAX_VERTICES:
        fail("画布最多包含 256 个节点")
    if not isinstance(groups, list) or len(groups) > 30 or not isinstance(edges, list) or len(edges) > MAX_EDGES:
        fail("画布最多包含 30 个池、4096 条连线")
    ids, result = set(), dict(version=1, vertices=[], groups=[], edges=[])
    def ident(value):
        if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", value) or value in ids:
            fail("组件标识无效或重复")
        ids.add(value)
        return value
    for g in groups:
        if not isinstance(g, dict):
            fail("无效的池")
        item = dict(id=ident(g.get("id")),
            name=validation.check_remark(g.get("name", "池"), "graph", "池名称"), locked=g.get("locked"))
        for axis in ("x", "y"):
            if axis in g:
                if type(g[axis]) not in (int, float) or not math.isfinite(g[axis]) or abs(g[axis]) > MAX_COORD:
                    fail("画布坐标超出范围")
                item[axis] = g[axis]
        for dimension, minimum in (("width", 220), ("height", 144)):
            if dimension in g:
                size = g[dimension]
                if type(size) not in (int, float) or not math.isfinite(size) or not minimum <= size <= 2 * MAX_COORD:
                    fail("池尺寸超出范围")
                item[dimension] = size
        result["groups"].append(item)
    gids = {g["id"] for g in result["groups"]}
    for v in vertices:
        if not isinstance(v, dict) or v.get("kind") not in ("entry", "node", "target", "direct"):
            fail("无效的节点类型")
        item = dict(id=ident(v.get("id")), kind=v["kind"], group=v.get("group"), disabled=v.get("disabled", False))
        if "legacy_section" in v:
            if not isinstance(v["legacy_section"], str) or not 1 <= len(v["legacy_section"]) <= 64:
                fail("无效的历史分段标识")
            item["legacy_section"] = v["legacy_section"]
        if type(item["disabled"]) is not bool or (item["group"] is not None and
                (not isinstance(item["group"], str) or item["group"] not in gids)):
            fail("节点所属池或禁用状态无效")
        if item["group"] and item["kind"] not in ("node", "direct"):
            fail("入口与目标不能放入池")
        for axis in ("x", "y"):
            coord = v.get(axis, 40)
            if type(coord) not in (int, float) or not math.isfinite(coord) or abs(coord) > MAX_COORD:
                fail("画布坐标超出范围")
            item[axis] = coord
        if item["kind"] == "target":
            item["target_id"] = database.get_target(c, v.get("target_id"))["id"]
        elif item["kind"] != "direct":
            n = database.get_node(c, v.get("node_id"))
            if n["deleting"]:
                fail(f"{n['name']} 正在删除")
            if item["group"] and (n["desired_mode"] != "active" or n["installed_agent"] != "active" or n["manual_required"]):
                fail(f"{n['name']} 不是已确认的主动节点，不能加入池")
            item["node_id"] = n["id"]
        result["vertices"].append(item)
    by_id = {v["id"]: v for v in result["vertices"]}
    for g in result["groups"]:
        members = [v for v in result["vertices"] if v["group"] == g["id"]]
        if len(members) > 32 or (complete and not members):
            fail(f"池「{g['name']}」需要包含 1 至 32 个成员", "group:" + g["id"])
        if g["locked"] is not None and not any(v["id"] == g["locked"] and not v["disabled"] for v in members):
            fail("锁定成员必须存在且未禁用")
    seen = set()
    for edge in edges:
        if not isinstance(edge, list) or len(edge) != 2 or any(not isinstance(x, str) or x not in ids for x in edge):
            fail("连线引用了不存在的组件")
        a, b = edge
        if a == b or tuple(edge) in seen:
            fail("不能重复连线或连接节点自身", "edge:" + a)
        if by_id.get(a, {}).get("kind") == "target" or by_id.get(b, {}).get("kind") == "entry":
            fail("入口不能接收入站连线，目标不能继续转发", "edge:" + a)
        if (by_id.get(a, {}).get("group") or a) == (by_id.get(b, {}).get("group") or b):
            fail("同一池的成员不能串行连接", "edge:" + a)
        seen.add(tuple(edge)); result["edges"].append(edge[:])
    result["edges"] = [list(e) for e in connections(result)]
    for kind in ("entry", "target"):
        count = sum(v["kind"] == kind for v in result["vertices"])
        if count > 1 or (complete and kind == "target" and count != 1):
            fail("每个画布只允许一个入口和一个目标")
    if complete and len(vertices) > 1 and not endpoints(result)[0]:
        fail("请添加入口节点")
    # Kahn's algorithm also detects cycles outside the currently selected path.
    components = gids | {v["id"] for v in result["vertices"] if not v["group"]}
    incoming = dict.fromkeys(components, 0)
    adj = {i: [] for i in components}
    for a, b in result["edges"]:
        adj[a].append(b); incoming[b] += 1
    queue = [i for i in components if not incoming[i]]
    for i in queue:
        for nxt in adj[i]:
            incoming[nxt] -= 1
            if not incoming[nxt]: queue.append(nxt)
    if len(queue) != len(components): fail("拓扑存在循环连接")
    if complete:
        path = find_path(result)
        if not path: fail("没有满足池锁定、禁用和节点不重复约束的入口到目标路径")
        entry, target = endpoints(result)
        reachable = {entry or target}
        for i in queue:
            if i in reachable: reachable.update(adj[i])
        back = {target}
        for i in reversed(queue):
            if any(n in back for n in adj[i]): back.add(i)
        stranded = [v["id"] for v in result["vertices"] if not v["disabled"] and
                    ((v["group"] or v["id"]) not in reachable or (v["group"] or v["id"]) not in back)]
        if stranded: fail("节点未连接到完整路径：" + "、".join(stranded), "vertex:" + stranded[0])
    return result


def find_path(graph, *, required=(), prefix=(), allowed=None, shuffle=False, preferred=()):
    """Randomized bounded DFS. Does not enumerate exponentially many paths."""
    vertices = {v["id"]: v for v in graph["vertices"]}
    entry, target = endpoints(graph)
    if not target: return None
    adj = {i: [] for i in vertices}
    edges = expanded_edges(graph)
    for a, b in edges: adj[a].append(b)
    locked = {g["id"]: g.get("locked") for g in graph["groups"]}
    # Prune vertices with no route to the destination before exploring branches.
    back, pending = {target}, [target]
    reverse = {i: [] for i in vertices}
    for a, b in edges: reverse[b].append(a)
    for i in pending:
        for n in reverse[i]:
            if n not in back: back.add(n); pending.append(n)
    visits = 0
    def walk(i, path, nodes, groups):
        nonlocal visits
        visits += 1
        if visits > MAX_VISITS: fail("路径组合过多，请简化拓扑")
        v = vertices[i]; group = v.get("group"); nid = v.get("node_id")
        if i not in back or i in path or v.get("disabled") or (nid and nid in nodes): return None
        if group and (group in groups or locked.get(group) not in (None, i)): return None
        if allowed is not None and not allowed(v): return None
        if len(path) < len(prefix) and prefix[len(path)] != i: return None
        path = path + [i]
        if len(path) > 64: return None
        if i == target: return path if set(required) <= set(path) and len(path) >= len(prefix) else None
        choices = list(adj[i])
        if shuffle: random.SystemRandom().shuffle(choices)
        elif preferred:
            continuation = preferred[preferred.index(i)+1:] if i in preferred else preferred
            choices.sort(key=lambda x: continuation.index(x) if x in continuation else len(continuation))
        for n in choices:
            result = walk(n, path, nodes | ({nid} if nid else set()), groups | ({group} if group else set()))
            if result: return result
        return None
    return walk(entry or target, [], set(), set())


def check_path(graph, path):
    if not isinstance(path, list) or not path or any(not isinstance(x, str) for x in path):
        fail("请选好完整路径", "path")
    found = find_path(graph, prefix=path)
    if found != path: fail("所选路径无法到达目标，或违反池与节点约束", "path")
    return path


def compile_plan(graph, path, port=None):
    check_path(graph, path)
    vertices = {v["id"]: v for v in graph["vertices"]}
    entry, target = endpoints(graph)
    stages = [dict(id=i, kind="node", node_id=vertices[i]["node_id"],
                   **({"legacy_section":vertices[i]["legacy_section"]} if "legacy_section" in vertices[i] else {}))
              for i in path if vertices[i]["kind"] == "node"]
    sections = [s.get("legacy_section", s["id"]) for s in stages]
    if len(sections) != len(set(sections)): fail("所选路径存在重复历史分段")
    return dict(entry=vertices[entry]["node_id"] if entry else None,
        target=vertices[target]["target_id"], port=validation.check_port(port, "port") if port not in (None, "") else None,
        stages=stages,
        graph=copy.deepcopy(graph), graph_path=path[:])


def from_legacy(c, plan, selected=None):
    if "graph" in plan:
        graph = copy.deepcopy(plan["graph"])
        graph["edges"] = [list(e) for e in connections(graph)]
        return graph, list(plan.get("graph_path", []))
    selected = selected or {}
    graph = dict(version=1, vertices=[], groups=[], edges=[])
    path, previous = [], []
    if plan.get("entry"):
        graph["vertices"].append(dict(id="entry", kind="entry", node_id=plan["entry"], x=40, y=100, group=None, disabled=False))
        path.append("entry"); previous = ["entry"]
    for index, st in enumerate(plan.get("stages", []), 1):
        sid = st["id"] if re.fullmatch(r"[A-Za-z0-9_-]{1,40}", st["id"]) else hashlib.sha256(st["id"].encode()).hexdigest()[:24]
        if st["kind"] == "pool":
            p = c.execute("SELECT name FROM pools WHERE id=?", (st["pool_id"],)).fetchone()
            group = "g-" + sid
            graph["groups"].append(dict(id=group, name=p[0] if p else "池", locked=(sid+"-"+str(st["locked"])) if st.get("locked") is not None else None))
            options = st["order"]
        else: group, options = None, [st["node_id"]]
        current = []
        for offset, nid in enumerate(options):
            vid = sid + "-" + str(nid) if group else sid
            v = dict(id=vid, kind="node" if nid else "direct", group=group,
                disabled=nid in st.get("disabled", []), x=40+index*260, y=100+offset*90, legacy_section=st["id"])
            if nid: v["node_id"] = nid
            graph["vertices"].append(v); current.append(vid)
            if not group or selected.get(st["id"], st.get("default", next(iter(options), None))) == nid: path.append(vid)
        graph["edges"].extend([[a,b] for a in previous for b in current]); previous = current
    if plan.get("target"):
        graph["vertices"].append(dict(id="target", kind="target", target_id=plan["target"], group=None, disabled=False,
            x=40+(len(plan.get("stages", []))+1)*260, y=100))
        graph["edges"].extend([[a,"target"] for a in previous]); path.append("target")
    return arrange(graph), path


def upgrade_plan(c, plan, selected=None):
    if plan and "graph" not in plan:
        plan["graph"], plan["graph_path"] = from_legacy(c, plan, selected)
    return plan


def migrate(c):
    if c.execute("PRAGMA user_version").fetchone()[0] >= 500: return
    import settings
    database.backup_database(c, settings.DB_PATH + ".pre-0.5.0.bak")
    with c:
        c.execute("BEGIN IMMEDIATE")
        c.execute("CREATE TABLE IF NOT EXISTS link_graphs(link_id INTEGER PRIMARY KEY REFERENCES links(id) ON DELETE CASCADE, draft TEXT, last_attempt INTEGER NOT NULL DEFAULT 0, blocked TEXT NOT NULL DEFAULT '{}')")
        database.set_setting(c, "archived_pools", json.dumps([r[0] for r in c.execute("SELECT id FROM pools")]))
        for link in c.execute("SELECT * FROM links").fetchall():
            selected = json.loads(link["selected"])
            plan = upgrade_plan(c, json.loads(link["plan"]), selected)
            applied = json.loads(link["applied"]) if link["applied"] else None
            if applied: upgrade_plan(c, applied["plan"], selected)
            job = json.loads(link["job"]) if link["job"] else None
            if job:
                if job.get("plan"): upgrade_plan(c, job["plan"], job.get("selected"))
                if job.get("previous"): upgrade_plan(c, job["previous"]["plan"], job["previous"].get("selected"))
                if job.get("previous_applied"):
                    previous = json.loads(job["previous_applied"])
                    upgrade_plan(c, previous["plan"], selected); job["previous_applied"] = json.dumps(previous)
            c.execute("UPDATE links SET plan=?,applied=?,job=? WHERE id=?", (json.dumps(plan),json.dumps(applied) if applied else None,json.dumps(job) if job else None,link["id"]))
        c.execute("PRAGMA user_version=500")


def decorate(c, link, result):
    graph, path = from_legacy(c, json.loads(link["plan"]), json.loads(link["selected"]))
    applied = json.loads(link["applied"])["plan"] if link["applied"] else None
    active_graph, active_path = from_legacy(c, applied, json.loads(link["selected"])) if applied else (None, [])
    row = c.execute("SELECT draft FROM link_graphs WHERE link_id=?", (link["id"],)).fetchone()
    result.update(graph=graph, graph_path=path, active_graph=active_graph, active_path=active_path,
                  draft=json.loads(row[0]) if row and row[0] else None)


def archived_pools(c):
    return set(json.loads(database.setting(c, "archived_pools") or "[]"))


def references(c, nid, pools_only=False):
    found = []
    for link in c.execute("SELECT * FROM links"):
        plans = [json.loads(link["plan"])]
        if link["applied"]: plans.append(json.loads(link["applied"])["plan"])
        if link["job"]:
            job = json.loads(link["job"])
            if job.get("plan"): plans.append(job["plan"])
        row = c.execute("SELECT draft FROM link_graphs WHERE link_id=?",(link["id"],)).fetchone()
        if row and row[0]: plans.append(json.loads(row[0]))
        if any(v.get("node_id") == nid and (not pools_only or v.get("group"))
               for p in plans for v in p.get("graph", {}).get("vertices", [])):
            found.append("链路："+link["name"])
    return found


def probe_edges(graph):
    vertices = {v["id"]:v for v in graph["vertices"]}
    adj = {i:[] for i in vertices}
    for a,b in expanded_edges(graph):
        if not vertices[a].get("disabled") and not vertices[b].get("disabled"): adj[a].append(b)
    result = set()
    for a,v in vertices.items():
        if not v.get("node_id") or v.get("disabled"): continue
        pending, seen = list(adj[a]), set()
        for b in pending:
            if b in seen: continue
            seen.add(b)
            if vertices[b].get("node_id"):
                if v["node_id"] != vertices[b]["node_id"]: result.add((v["node_id"],vertices[b]["node_id"]))
            elif vertices[b]["kind"] == "direct": pending.extend(adj[b])
    return result


def save(c, body, ident=None):
    import pools
    old = pools.get(c, "links", ident) if ident else None
    if old and (body.get("revision") != old["revision"] or old["state"] == "deleting"):
        raise errors.UserError("链路已变化，请刷新后重试", status=409)
    if old and old["job"]: raise errors.UserError("请等待当前切换确认后再编辑", status=409)
    apply = body.get("apply", False)
    if type(apply) is not bool: fail("无效的应用操作")
    graph = validate(c, body.get("graph"), complete=apply)
    name = validation.check_remark(body.get("name", old["name"] if old else ""), "name", "名称")
    group = database.check_group(c, "links", body.get("group_id", old["group_id"] if old else None))
    port = body.get("port")
    if port not in (None, ""): port = validation.check_port(port, "port")
    draft = dict(graph=graph, name=name, group_id=group, port=port)
    if apply:
        if not body.get("switch_only"): graph = arrange(graph)
        plan = compile_plan(graph, body.get("path"), port)
        if old and body.get("switch_only"):
            current = pools.runtime_plan(old)
            if not current or from_legacy(c, current, json.loads(old["selected"]))[0] != graph:
                raise errors.UserError("运行拓扑已变化，请重新选择", status=409)
            if plan["port"] != current["port"]:
                fail("运行选路不能修改入口端口，请进入编辑模式")
            ready_path(c, graph, plan["graph_path"])
            picked = {st["id"]: st["node_id"] for st in plan["stages"]}
            path = [v["node_id"] for i in plan["graph_path"] for v in graph["vertices"] if v["id"] == i and v.get("node_id")]
            pools.begin(c, old, plan, picked, path, "手动选择路径", update_plan=False,
                        target_snapshot=json.loads(old["applied"])["target"])
            c.execute("UPDATE links SET revision=revision+1 WHERE id=?", (ident,))
            return dict(id=ident)
        ready_path(c, graph, plan["graph_path"])
        result = pools.link_save(c, dict(plan=plan, name=name, group_id=group, revision=body.get("revision"), apply=True), ident)
        ident = result["id"]
        c.execute("INSERT INTO link_graphs(link_id,draft) VALUES (?,NULL) ON CONFLICT(link_id) DO UPDATE SET draft=NULL", (ident,))
    else:
        if not ident:
            ident = pools.link_save(c, dict(name=name, group_id=group, plan=dict(entry=None,target=None,stages=[]), apply=False))["id"]
        else: c.execute("UPDATE links SET revision=revision+1 WHERE id=?", (ident,))
        c.execute("INSERT INTO link_graphs(link_id,draft) VALUES (?,?) ON CONFLICT(link_id) DO UPDATE SET draft=excluded.draft", (ident,json.dumps(draft)))
    return dict(id=ident)


def ready_path(c, graph, path, automatic=False, blocked=()):
    import pools
    nodes = {r["id"]: dict(r) for r in c.execute("SELECT * FROM nodes")}
    def allowed(v):
        if not v.get("node_id"): return True
        n = nodes.get(v["node_id"])
        if not n or n["deleting"] or n["manual_required"] or n["id"] in blocked: return False
        if n["desired_mode"] == "active": return pools.ready(n, int(time.time()))
        return not automatic or bool(n["wake_enabled"] and n["wake_port"])
    if path is None: return allowed
    for v in graph["vertices"]:
        if v["id"] in path and not allowed(v): fail("路径包含未就绪节点：" + str(v.get("node_id", v["id"])), "vertex:"+v["id"])


def preview(c, body):
    graph = validate(c, body.get("graph"))
    prefix, required, preferred = body.get("prefix", []), body.get("required", []), body.get("preferred", [])
    if any(not isinstance(x,list) for x in (prefix,required,preferred)) or any(not isinstance(x,str) for x in prefix+required+preferred): fail("无效的路径选择")
    allowed = ready_path(c, graph, None) if body.get("ready", True) else None
    path = find_path(graph, prefix=prefix, required=required, allowed=allowed, preferred=preferred)
    if not path: fail("没有符合当前选择的可用路径", "path")
    # Only expose next hops which can complete the path under the same constraints.
    base = list(prefix or [path[0]])
    edges = expanded_edges(graph)
    while True:
        options = [b for a,b in edges if a == base[-1] and find_path(graph,prefix=base+[b],required=required,allowed=allowed)]
        if not body.get("advance") or not options: break
        next_hop = options[0] if len(options) == 1 else next((i for i in required if i in options), None)
        if next_hop is None:
            continuation = preferred[preferred.index(base[-1])+1:] if base[-1] in preferred else preferred
            next_hop = next((i for i in continuation if i in options), None)
        if next_hop is None: break
        base.append(next_hop)
    return dict(graph=arrange(graph) if body.get("arrange") else graph, path=path, options=options,
                prefix=base, complete=base[-1] == endpoints(graph)[1])


def failover(c, link, now):
    import pools
    plan = pools.runtime_plan(link)
    graph = plan["graph"]
    nodes = {r["id"]: dict(r) for r in c.execute("SELECT * FROM nodes")}
    live = [r[0] for r in c.execute("SELECT node_id FROM hops WHERE chain_id=?", (link["chain_id"],))]
    lost = [nid for nid in live if nodes[nid]["desired_mode"] == "active" and nodes[nid]["last_seen"] is not None and now-nodes[nid]["last_seen"] >= pools.OFFLINE_AFTER]
    if not lost:
        if link["state"] == "error" and link["error"].startswith("灾备等待："):
            c.execute("UPDATE links SET state='active',error='' WHERE id=?", (link["id"],))
        return
    c.execute("INSERT OR IGNORE INTO link_graphs(link_id) VALUES (?)", (link["id"],))
    state = c.execute("SELECT * FROM link_graphs WHERE link_id=?", (link["id"],)).fetchone()
    if now-state["last_attempt"] < 30: return
    blocked = {int(k):v for k,v in json.loads(state["blocked"]).items() if v>now}
    for nid in json.loads(link["failures"]): blocked[nid] = now+120
    c.execute("UPDATE link_graphs SET last_attempt=?,blocked=? WHERE link_id=?", (now,json.dumps(blocked),link["id"]))
    c.execute("UPDATE links SET failures='[]' WHERE id=?", (link["id"],))
    try:
        path = find_path(graph, allowed=ready_path(c,graph,None,automatic=True,blocked=set(blocked)|set(lost)), shuffle=True)
        if not path: fail("没有可用的完整备用路径（请检查节点、锁定与被动唤醒）")
        new = compile_plan(graph,path,plan["port"])
        selected = {st["id"]:st["node_id"] for st in new["stages"]}
        by_id = {v["id"]:v for v in graph["vertices"]}
        pools.begin(c,link,new,selected,[by_id[i]["node_id"] for i in path if by_id[i].get("node_id")],
                    "节点持续失联，随机灾备",update_plan=False,target_snapshot=json.loads(link["applied"])["target"])
    except errors.UserError as exc:
        if exc.field and exc.field.startswith("capacity:"):
            blocked[int(exc.field.split(":")[1])] = now+120
            c.execute("UPDATE link_graphs SET blocked=? WHERE link_id=?", (json.dumps(blocked),link["id"]))
        text = "灾备等待：" + str(exc)
        if text != link["error"]: pools.event(c,link["id"],text)
        c.execute("UPDATE links SET state='error',error=? WHERE id=?", (text,link["id"]))
