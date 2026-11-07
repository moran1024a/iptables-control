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
import node_addresses

MAX_VERTICES = 256
# A DAG of this size cannot contain more edges. Old pool connections can
# expand beyond the former 4096 component-edge limit during migration.
MAX_EDGES = MAX_VERTICES * (MAX_VERTICES - 1) // 2
MAX_VISITS = 8192
MAX_COORD = 1000000


def fail(message, field="graph"):
    raise errors.UserError(message, field)


def endpoints(graph):
    return tuple(next((v["id"] for v in graph["vertices"] if v["kind"] == k), None)
                 for k in ("entry", "target"))


def flatten(graph):
    """Read old pool graphs as explicit node connections, preserving bound IPs."""
    if not graph or not isinstance(graph, dict): return graph
    groups, vertices, edges = graph.get('groups', []), graph.get('vertices'), graph.get('edges')
    if not isinstance(groups, list) or not isinstance(vertices, list) or not isinstance(edges, list):
        fail('无效的拓扑结构')
    if len(vertices) > MAX_VERTICES: fail('画布最多包含 256 个节点')
    if not groups and not any(isinstance(v, dict) and 'group' in v for v in vertices):
        return {k: v for k, v in graph.items() if k != 'groups'}
    if any(not isinstance(g, dict) or not isinstance(g.get('id'), str) or
           not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', g['id']) for g in groups):
        fail('无效的旧拓扑')
    if len({g['id'] for g in groups}) != len(groups): fail('重复的旧拓扑组件')
    if not isinstance(graph.get('bindings', {}), dict): fail('无效的地址映射')
    result = copy.deepcopy(graph)
    members = {g['id']: [] for g in groups}
    locked = {g['id']: g.get('locked') for g in groups}
    owners = {}
    for v in result['vertices']:
        if not isinstance(v, dict) or not isinstance(v.get('id'), str): fail('无效的节点')
        if v['id'] in owners or v['id'] in locked: fail('重复的旧拓扑组件')
        group = v.pop('group', None)
        if group is not None and (not isinstance(group, str) or group not in locked): fail('无效的旧节点归属')
        owners[v['id']] = group or v['id']
        members.setdefault(group or v['id'], []).append(v['id'])
        if group and locked[group] not in (None, v['id']): v['disabled'] = True
    for group, selection in locked.items():
        if selection is not None and selection not in members[group]: fail('无效的旧节点锁定')
    expanded, bindings, seen = [], {}, set()
    outgoing = {i: set() for i in members}
    for edge in edges:
        if not isinstance(edge, list) or len(edge) != 2 or any(not isinstance(i, str) for i in edge): fail('无效的连线')
        source, target = (owners.get(i, i) for i in edge)
        if source not in members or target not in members: fail('连线引用了不存在的节点')
        outgoing[source].add(target)
        mapping = graph.get('bindings', {}).get(source+'/'+target, {})
        if not isinstance(mapping, dict) or any(not isinstance(mapping.get(side, {}), dict) for side in ('from','to')):
            fail('无效的地址映射')
        for a in members[source]:
            for b in members[target]:
                if (a,b) in seen: continue
                seen.add((a,b))
                expanded.append([a,b])
                if graph.get('version') == 2:
                    bindings[a+'/'+b] = {side: {vid: mapping.get(side, {})[vid]} if vid in mapping.get(side, {}) else {}
                                        for side, vid in (('from', a), ('to', b))}
    # Even an empty legacy container must not hide an invalid cycle.
    degree = dict.fromkeys(members, 0)
    for targets in outgoing.values():
        for target in targets: degree[target] += 1
    queue = [i for i in members if not degree[i]]
    for source in queue:
        for target in outgoing[source]:
            degree[target] -= 1
            if not degree[target]: queue.append(target)
    if len(queue) != len(members): fail('拓扑存在循环连接')
    result.pop('groups', None)
    result['edges'] = expanded
    if graph.get('version') == 2: result['bindings'] = bindings
    return result


def connections(graph):
    return list(dict.fromkeys(tuple(e) for e in flatten(graph)['edges']))


def expanded_edges(graph):
    return connections(graph)


def snapshot(graph):
    """Compare bound routing data, not the mutable address inventory or labels."""
    graph = flatten(graph)
    if not graph or graph.get('version') != 2:
        return graph
    result = copy.deepcopy(graph)
    edges = {'/'.join(e) for e in connections(result)}
    result['bindings'] = {k: v for k, v in result.get('bindings', {}).items() if k in edges}
    pairs = {a['id']+'/'+b['id'] for a in result['vertices'] if a['kind']=='entry'
             for b in result['vertices'] if b['kind']=='target'}
    result['ports'] = {k: v for k, v in result.get('ports', {}).items() if k in pairs}
    used = node_addresses.references(result)
    result['address_book'] = {vid: {ident: node_addresses.routing(address)
        for ident, address in addresses.items() if ident in used.get(vid, set())}
        for vid, addresses in result.get('address_book', {}).items() if vid in used}
    return result


def documents(c):
    """All saved, running and in-flight plans, including rollback and drafts."""
    for row in c.execute('''SELECT l.name,l.plan,l.applied,l.job,g.draft,
            t.graph,t.applied_graph,b.graph AS batch_graph FROM links l
        LEFT JOIN link_graphs g ON g.link_id=l.id
        LEFT JOIN topology_canvases t ON t.root_id=l.id
        LEFT JOIN link_batches b ON b.root_id=l.id'''):
        docs = [json.loads(row[key]) for key in ('plan', 'draft') if row[key]]
        if row['applied']: docs.append(json.loads(row['applied'])['plan'])
        if row['job']:
            job = json.loads(row['job'])
            docs.extend((job, job.get('plan', {}), (job.get('previous') or {}).get('plan', {})))
            if job.get('previous_applied'): docs.append(json.loads(job['previous_applied'])['plan'])
        docs.extend({'graph': json.loads(row[key])} for key in ('graph', 'applied_graph', 'batch_graph') if row[key])
        for doc in docs: yield row['name'], doc


def validate_bindings(c, raw, graph, complete):
    bindings = raw.get('bindings', {})
    if not isinstance(bindings, dict): fail('无效的地址映射')
    graph['bindings'], graph['address_book'] = {}, {}
    ports = raw.get('ports', {})
    if not isinstance(ports, dict): fail('无效的入口端口列表')
    entries = [v['id'] for v in graph['vertices'] if v['kind'] == 'entry']
    targets = [v['id'] for v in graph['vertices'] if v['kind'] == 'target']
    pairs = {a + '/' + b for a in entries for b in targets}
    graph['ports'] = {key: validation.check_port(value, 'port') for key, value in ports.items() if key in pairs}
    for v in graph['vertices']:
        if v.get('node_id'):
            graph['address_book'][v['id']] = {a['id']: a for a in node_addresses.listing(c, database.get_node(c, v['node_id']))}
            if complete and not v['disabled'] and not graph['address_book'][v['id']]:
                fail('请先在节点编辑页设置 IP 地址', 'vertex:' + v['id'])
        if v['kind'] == 'entry' and not v.get('address') and len(graph['address_book'][v['id']]) == 1:
            v['address'] = next(iter(graph['address_book'][v['id']]))
        if v['kind'] in ('entry', 'target') and v['disabled']:
            fail('入口和目标不能禁用，请从画布移除', 'vertex:' + v['id'])
        if v['kind'] == 'entry' and complete and v.get('address') not in graph['address_book'][v['id']]:
            fail('请选择入口接收地址', 'vertex:' + v['id'])
        if v['kind'] == 'entry' and complete and not node_addresses.ready(graph['address_book'][v['id']][v['address']]):
            fail('入口地址待识别，请先同步 Agent 或在地址高级设置中选择本机地址、网卡', 'vertex:' + v['id'])
    for source, target in connections(graph):
        key = node_addresses.edge_key(source, target)
        mapping = bindings.get(key, {})
        if not isinstance(mapping, dict): fail('无效的连线地址映射')
        clean = {}
        for side, component in (('from', source), ('to', target)):
            values = mapping.get(side, {})
            if not isinstance(values, dict): fail('无效的连线地址映射')
            clean[side] = {}
            for v in graph['vertices']:
                if v['id'] != component or not v.get('node_id'): continue
                available = graph['address_book'][v['id']]
                ident = values.get(v['id'])
                if ident is None and len(available) == 1: ident = next(iter(available))
                if ident not in available:
                    if complete and not v['disabled']:
                        name = database.get_node(c, v['node_id'])['name']
                        direction = '发出' if side == 'from' else '接收'
                        fail(f'请点击「{name}」的{direction} IP 连接点', 'vertex:' + v['id'])
                else:
                    if complete and not v['disabled'] and not node_addresses.ready(available[ident]):
                        fail(f"地址 {available[ident]['host']} 待识别，请先同步 Agent 或补全地址高级设置", 'vertex:' + v['id'])
                    clean[side][v['id']] = ident
        graph['bindings'][key] = clean


def compile_routing(graph, path):
    graph = flatten(graph)
    vertices = {v['id']: v for v in graph['vertices']}
    book = graph['address_book']
    routes = []
    incoming = []
    def chosen(a, b, vid, side):
        key = node_addresses.edge_key(a, b)
        ident = graph['bindings'].get(key, {}).get(side, {}).get(vid)
        try: return book[vid][ident]
        except KeyError: fail('路径缺少进出地址映射', 'vertex:' + vid)
    for index, vid in enumerate(path):
        vertex = vertices[vid]
        if not vertex.get('node_id'): continue
        if vertex['kind'] == 'entry':
            try: inc = book[vid][vertex['address']]
            except KeyError: fail('请选择入口接收地址', 'vertex:' + vid)
        else: inc = chosen(path[index-1], vid, vid, 'to')
        out = chosen(vid, path[index+1], vid, 'from')
        routes.append(node_addresses.route_token(inc, out))
        incoming.append(inc['host'])
    return dict(routing=routes, hop_addresses=incoming, ingress_host=incoming[0] if incoming else None)


def arrange(graph):
    """Lay out nodes by dependency, aligning connected node centres."""
    graph = copy.deepcopy(flatten(graph))
    nodes = {v['id']: v for v in graph['vertices']}
    edges = connections(graph)
    graph["edges"] = [list(e) for e in edges]
    incoming = {i: [] for i in nodes}
    outgoing = {i: [] for i in nodes}
    for a, b in edges:
        incoming[b].append(a); outgoing[a].append(b)
    degree = {i: len(incoming[i]) for i in nodes}
    queue = [i for i in nodes if not degree[i]]
    ranks = dict.fromkeys(nodes, 0)
    for i in queue:
        for b in outgoing[i]:
            ranks[b] = max(ranks[b], ranks[i] + 1)
            degree[b] -= 1
            if not degree[b]: queue.append(b)
    if len(queue) != len(nodes): fail("拓扑存在循环连接")
    final_rank = max(ranks.values(), default=0)
    for v in graph['vertices']:
        if v['kind'] == 'target': ranks[v['id']] = final_rank
    layers = {}
    for i in queue: layers.setdefault(ranks[i], []).append(i)
    card_height = lambda v: 62 + 24 * max(1, len(graph.get('address_book', {}).get(v['id'], {})))
    heights = {i: card_height(v) for i, v in nodes.items()}
    if not layers: return graph
    original_y = {i: v.get('y', 0) for i, v in nodes.items()}
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
                desired[i] = (positions[(len(positions) - 1) // 2] + positions[len(positions) // 2]) / 2
            else: desired[i] = axis
        layer.sort(key=lambda i: (desired[i], original_y[i]))
        bottom = -math.inf
        for i in layer:
            centers[i] = max(desired[i], bottom + 48 + heights[i] / 2)
            bottom = centers[i] + heights[i] / 2
        # Keep colliding siblings centred on their neighbours instead of
        # accumulating a downward offset at each column.
        shift = sum(centers[i] - desired[i] for i in layer) / len(layer)
        for i in layer: centers[i] -= shift

    for rank in sorted(r for r in layers if r > anchor): align(rank, incoming)
    for rank in sorted((r for r in layers if r < anchor), reverse=True): align(rank, outgoing)
    shift = 60 - min(centers[i] - heights[i] / 2 for i in nodes)
    x = 40
    for rank, layer in sorted(layers.items()):
        for i in layer:
            y = centers[i] + shift - heights[i] / 2
            nodes[i].update(x=x, y=round(y))
        x += 380
    return graph


def validate(c, raw, complete=True):
    if not isinstance(raw, dict) or type(raw.get("version")) is not int or raw["version"] not in (1, 2):
        fail("无效的拓扑版本")
    raw = flatten(raw)
    vertices, edges = (raw.get(k) for k in ("vertices", "edges"))
    if not isinstance(vertices, list) or len(vertices) > MAX_VERTICES:
        fail("画布最多包含 256 个节点")
    if not isinstance(edges, list) or len(edges) > MAX_EDGES:
        fail(f"画布最多包含 {MAX_EDGES} 条连线")
    ids, result = set(), dict(version=raw['version'], vertices=[], edges=[])
    def ident(value):
        if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", value) or value in ids:
            fail("组件标识无效或重复")
        ids.add(value)
        return value
    for v in vertices:
        if not isinstance(v, dict) or v.get("kind") not in ("entry", "node", "target", "direct"):
            fail("无效的节点类型")
        item = dict(id=ident(v.get("id")), kind=v["kind"], disabled=v.get("disabled", False))
        if "legacy_section" in v:
            if not isinstance(v["legacy_section"], str) or not 1 <= len(v["legacy_section"]) <= 64:
                fail("无效的历史分段标识")
            item["legacy_section"] = v["legacy_section"]
        if type(item['disabled']) is not bool: fail('节点禁用状态无效')
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
            item["node_id"] = n["id"]
            for side in ('in_address', 'out_address'):
                if side in v:
                    if not isinstance(v[side], str) or not re.fullmatch(r'[A-Za-z0-9_-]{0,48}', v[side]):
                        fail('无效的节点 IP 选择')
                    item[side] = v[side]
            if raw['version'] == 2 and item['kind'] == 'entry':
                item['address'] = v.get('address')
        result["vertices"].append(item)
    by_id = {v["id"]: v for v in result["vertices"]}
    seen = set()
    for edge in edges:
        if not isinstance(edge, list) or len(edge) != 2 or any(not isinstance(x, str) or x not in ids for x in edge):
            fail("连线引用了不存在的组件")
        a, b = edge
        if a == b or tuple(edge) in seen:
            fail("不能重复连线或连接节点自身", "edge:" + a)
        if by_id.get(a, {}).get("kind") == "target" or by_id.get(b, {}).get("kind") == "entry":
            fail("入口不能接收入站连线，目标不能继续转发", "edge:" + a)
        seen.add(tuple(edge)); result["edges"].append(edge[:])
    result["edges"] = [list(e) for e in connections(result)]
    for kind in ("entry", "target"):
        count = sum(v["kind"] == kind for v in result["vertices"])
        if raw['version'] == 1 and count > 1:
            fail("每个画布只允许一个入口和一个目标")
        if complete and not count and (kind == 'target' or raw['version'] == 2):
            fail('请至少添加一个' + ('入口' if kind == 'entry' else '目标'))
    if raw['version'] == 2:
        validate_bindings(c, raw, result, complete)
        if sum(v['kind'] == 'entry' for v in vertices) * sum(v['kind'] == 'target' for v in vertices) > 64:
            fail('每张拓扑最多生成 64 个入口与目标组合')
    if complete and len(vertices) > 1 and not endpoints(result)[0]:
        fail("请添加入口节点")
    # Kahn's algorithm also detects cycles outside the currently selected path.
    components = set(by_id)
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
        if result['version'] == 2:
            for source in (v for v in result['vertices'] if v['kind'] == 'entry'):
                for destination in (v for v in result['vertices'] if v['kind'] == 'target'):
                    if not find_path(result, entry=source['id'], target=destination['id']):
                        name = database.get_node(c, source['node_id'])['name']
                        target_name = database.get_target(c, destination['target_id'])['name']
                        fail(f'入口「{name}」无法到达目标「{target_name}」', 'vertex:'+source['id'])
        elif not find_path(result): fail("没有满足禁用和节点不重复约束的入口到目标路径")
        entry, target = endpoints(result)
        reachable = {v['id'] for v in result['vertices'] if v['kind'] == 'entry'} or {target}
        for i in queue:
            if i in reachable: reachable.update(adj[i])
        back = {v['id'] for v in result['vertices'] if v['kind'] == 'target'}
        for i in reversed(queue):
            if any(n in back for n in adj[i]): back.add(i)
        stranded = [v["id"] for v in result["vertices"] if not v["disabled"] and
                    (v['id'] not in reachable or v['id'] not in back)]
        if stranded:
            labels = []
            for ident in stranded:
                vertex = by_id[ident]
                name = (database.get_node(c, vertex['node_id'])['name'] if vertex.get('node_id') else
                        database.get_target(c, vertex['target_id'])['name'] if vertex.get('target_id') else 'direct')
                component = ident
                missing = ('入口无法到达' if component not in reachable else '')
                if component not in back: missing += ('；' if missing else '') + '无法到达目标'
                labels.append(f'{name}（{missing}）')
            fail("节点未连接到完整路径：" + "、".join(labels), "vertex:" + stranded[0])
    return result


def find_path(graph, *, entry=None, target=None, required=(), prefix=(), allowed=None, shuffle=False, preferred=(), first_hop=...):
    """Bounded DFS, pruning branches that cannot reach the remaining waypoints."""
    graph = flatten(graph)
    vertices = {v["id"]: v for v in graph["vertices"]}
    default_entry, default_target = endpoints(graph)
    entry, target = entry or default_entry, target or default_target
    required = set(required)
    if not target or not required <= vertices.keys(): return None
    adj = {i: [] for i in vertices}
    edges = expanded_edges(graph)
    for a, b in edges: adj[a].append(b)
    reverse = {i: [] for i in vertices}
    for a, b in edges: reverse[b].append(a)
    reachable = {}
    for waypoint in required | {target}:
        back, pending = {waypoint}, [waypoint]
        for i in pending:
            for n in reverse[i]:
                if n not in back: back.add(n); pending.append(n)
        reachable[waypoint] = back
    visits = 0
    def walk(i, path, nodes):
        nonlocal visits
        visits += 1
        if visits > MAX_VISITS: fail("路径组合过多，请简化拓扑")
        v = vertices[i]; nid = v.get("node_id")
        if i not in reachable[target] or i in path or v.get("disabled") or (nid and nid in nodes): return None
        if first_hop is not ... and v['kind'] in ('node', 'target') and not any(vertices[p]['kind'] == 'node' for p in path):
            if (i if v['kind'] == 'node' else None) != first_hop: return None
        if any(i not in reachable[r] for r in required.difference(path)): return None
        if allowed is not None and not allowed(v): return None
        if len(path) < len(prefix) and prefix[len(path)] != i: return None
        path = path + [i]
        if len(path) > 64: return None
        if i == target: return path if required <= set(path) and len(path) >= len(prefix) else None
        choices = list(adj[i])
        if shuffle: random.SystemRandom().shuffle(choices)
        elif preferred:
            continuation = preferred[preferred.index(i)+1:] if i in preferred else preferred
            choices.sort(key=lambda x: continuation.index(x) if x in continuation else len(continuation))
        for n in choices:
            result = walk(n, path, nodes | ({nid} if nid else set()))
            if result: return result
        return None
    return walk(entry or target, [], set())


def entry_hop(graph, path):
    """The first forwarding node; direct targets and legacy bypasses use None."""
    nodes = {v['id'] for v in graph['vertices'] if v['kind'] == 'node'}
    return next((i for i in path[1:] if i in nodes), None)


def entry_paths(graph, entry, *, prefix=(), required=(), preferred=None, allowed=None, direct=False, first_hop=...):
    """Select routes with one first hop, retaining unaffected downstream paths."""
    graph = flatten(graph)
    vertices = {v['id']: v for v in graph['vertices']}
    targets = [v['id'] for v in graph['vertices'] if v['kind'] == 'target']
    preferred = preferred or {}
    # Older clients can still send the terminal target in their selection.
    if prefix and prefix[-1] in targets:
        prefix, direct = prefix[:-1], True
    constrained = len(prefix) > 1 or bool(required) or direct
    # Only visit possible first forwarding nodes. A legacy direct component is
    # transparent; multiple direct targets belong to the same no-relay route.
    adjacency = {i: [] for i in vertices}
    for a, b in expanded_edges(graph): adjacency[a].append(b)
    hops, seen, queue = [], {entry}, list(adjacency.get(entry, ()))
    for i in queue:
        if i in seen: continue
        seen.add(i)
        if vertices[i]['kind'] == 'direct': queue.extend(adjacency[i])
        else:
            hop = i if vertices[i]['kind'] == 'node' else None
            if hop not in hops: hops.append(hop)
    current_hops = [entry_hop(graph, p) for p in preferred.values() if p]
    hops.sort(key=lambda hop: -current_hops.count(hop))
    if first_hop is not ...: hops = [hop for hop in hops if hop == first_hop]
    for hop in hops:
        selected = {}
        for target in targets:
            path = find_path(graph, entry=entry, target=target,
                             prefix=[*prefix, target] if direct else prefix,
                             required=required, preferred=preferred.get(target, ()),
                             allowed=allowed, first_hop=hop)
            if path: selected[target] = path
        if not selected: continue
        for target in targets:
            if target in selected: continue
            old = preferred.get(target, [])
            if constrained and old and entry_hop(graph, old) == hop and find_path(graph, entry=entry, target=target, prefix=old) == old:
                continue
            # Changing the first hop also moves targets outside the selected
            # downstream branch. Never leave them on another first hop.
            path = find_path(graph, entry=entry, target=target, preferred=old,
                             allowed=allowed, first_hop=hop)
            if not path: break
            selected[target] = path
        else:
            return [selected[t] for t in targets if t in selected]
    return []


def check_path(graph, path):
    if not isinstance(path, list) or not path or any(not isinstance(x, str) for x in path):
        fail("请选好完整路径", "path")
    vertices = {v['id']: v for v in graph['vertices']}
    if graph.get('version') == 2 and (vertices.get(path[0], {}).get('kind') != 'entry' or vertices.get(path[-1], {}).get('kind') != 'target'):
        fail('路径必须从入口到目标', 'path')
    entry, target = (path[0], path[-1]) if graph.get('version') == 2 else endpoints(graph)
    found = find_path(graph, entry=entry, target=target, prefix=path)
    if found != path: fail("所选路径无法到达目标，或包含禁用、重复节点", "path")
    return path


def compile_plan(graph, path, port=None):
    graph = flatten(graph)
    check_path(graph, path)
    vertices = {v["id"]: v for v in graph["vertices"]}
    entry, target = (path[0], path[-1]) if graph.get('version') == 2 else endpoints(graph)
    stages = [dict(id=i, kind="node", node_id=vertices[i]["node_id"],
                   **({"legacy_section":vertices[i]["legacy_section"]} if "legacy_section" in vertices[i] else {}))
              for i in path if vertices[i]["kind"] == "node"]
    sections = [s.get("legacy_section", s["id"]) for s in stages]
    if len(sections) != len(set(sections)): fail("所选路径存在重复历史分段")
    plan = dict(entry=vertices[entry]["node_id"] if entry else None,
        target=vertices[target]["target_id"], port=validation.check_port(port, "port") if port not in (None, "") else None,
        stages=stages,
        graph=copy.deepcopy(graph), graph_path=path[:])
    if graph.get('version') == 2:
        plan.update(compile_routing(graph, path))
    return plan


def from_legacy(c, plan, selected=None):
    if "graph" in plan:
        graph = copy.deepcopy(flatten(plan['graph']))
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


def migrate_nodes(c):
    """Convert saved topology metadata; installed rules and receipts stay intact."""
    if c.execute('PRAGMA user_version').fetchone()[0] >= 502: return
    import settings
    database.backup_database(c, settings.DB_PATH + '.pre-node-topology.bak')
    def convert(value):
        if isinstance(value, list): return [convert(v) for v in value]
        if not isinstance(value, dict): return value
        if value.get('version') in (1, 2) and 'vertices' in value and 'edges' in value:
            return flatten(value)
        result = {k: convert(v) for k, v in value.items()}
        if result.get('previous_applied'):
            result['previous_applied'] = json.dumps(convert(json.loads(result['previous_applied'])), ensure_ascii=False)
        return result
    with c:
        c.execute('BEGIN IMMEDIATE')
        for table, key, columns in (
            ('links', 'id', ('plan','applied','job','failure_scope','retained')),
            ('link_graphs', 'link_id', ('draft',)),
            ('topology_canvases', 'root_id', ('graph','applied_graph')),
            ('link_batches', 'root_id', ('graph',)),
        ):
            for row in c.execute(f'SELECT {key},{",".join(columns)} FROM {table}').fetchall():
                for column in columns:
                    if not row[column]: continue
                    old = json.loads(row[column])
                    new = convert(old)
                    if new != old:
                        c.execute(f'UPDATE {table} SET {column}=? WHERE {key}=?',
                                  (json.dumps(new, ensure_ascii=False), row[key]))
        c.execute('PRAGMA user_version=502')


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


def decorate(c, link, result, cache=None):
    graph, path = from_legacy(c, json.loads(link["plan"]), json.loads(link["selected"]))
    applied = json.loads(link["applied"])["plan"] if link["applied"] else None
    active_graph, active_path = from_legacy(c, applied, json.loads(link["selected"])) if applied else (None, [])
    row = c.execute("SELECT draft FROM link_graphs WHERE link_id=?", (link["id"],)).fetchone()
    result.update(graph=graph, graph_path=path, active_graph=active_graph, active_path=active_path,
                  draft=json.loads(row[0]) if row and row[0] else None)
    import topology_groups
    topology_groups.decorate(c, link, result, cache)
    view = copy.deepcopy(result['active_graph'] or result['graph'])
    if view.get('version') == 2:
        view['address_book'] = {v['id']: {a['id']: a for a in node_addresses.listing(c, database.get_node(c, v['node_id']))}
                                for v in view['vertices'] if v.get('node_id')}
    result['view_graph'] = arrange(view)


def archived_pools(c):
    return set(json.loads(database.setting(c, "archived_pools") or "[]"))


def references(c, nid, pools_only=False):
    return list(dict.fromkeys('链路：'+name for name, doc in documents(c)
        if any(v.get('node_id') == nid and (not pools_only or v.get('group'))
               for v in doc.get('graph', {}).get('vertices', []))))


def probe_edges(graph):
    graph = flatten(graph)
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
    import topology_groups
    if ident and topology_groups.canvas(c, ident) and body.get('graph', {}).get('version') != 2:
        fail('多入口拓扑请保留指定 IP 路由模式')
    if body.get("graph", {}).get("version") == 2:
        import topology_groups
        return topology_groups.save(c, body, ident)
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
        if v['kind'] == 'entry' and not automatic: return True
        if n["desired_mode"] == "active": return pools.ready(n, int(time.time()))
        return not automatic or bool(n["wake_enabled"] and n["wake_port"])
    if path is None: return allowed
    for v in graph["vertices"]:
        if v["id"] in path and not allowed(v):
            fail("路径包含未就绪节点：" + nodes.get(v.get('node_id'), {}).get('name', '已删除'), "vertex:"+v["id"])


def preview(c, body):
    if body.get('layout_only') is True:
        return dict(graph=arrange(validate(c, body.get('graph'), complete=False)))
    graph = validate(c, body.get("graph"))
    if graph.get('version') == 2:
        import topology_groups
        return topology_groups.preview(c, graph, body)
    prefix, required, preferred = body.get("prefix", []), body.get("required", []), body.get("preferred", [])
    if any(not isinstance(x,list) for x in (prefix,required,preferred)) or any(not isinstance(x,str) for x in prefix+required+preferred): fail("无效的路径选择")
    allowed = ready_path(c, graph, None) if body.get("ready", True) else None
    path = find_path(graph, prefix=prefix, required=required, allowed=allowed, preferred=preferred)
    if not path: fail("没有符合当前选择的可用路径", "path")
    # Only expose next hops which can complete the path under the same constraints.
    base = list(prefix or [path[0]])
    edges = expanded_edges(graph)
    target = endpoints(graph)[1]
    while True:
        options = [b for a,b in edges if a == base[-1] and find_path(graph,prefix=base+[b],required=required,allowed=allowed)]
        if target in options and (len(options) == 1 or body.get('finish') is True):
            base.append(target)
            options = []
            break
        if not body.get("advance") or not options: break
        next_hop = options[0] if len(options) == 1 else next((i for i in required if i in options), None)
        if next_hop is None:
            continuation = preferred[preferred.index(base[-1])+1:] if base[-1] in preferred else preferred
            next_hop = next((i for i in continuation if i in options), None)
        if next_hop is None: break
        base.append(next_hop)
    complete = base[-1] == target
    return dict(graph=arrange(graph) if body.get("arrange") else graph, path=base if complete else path,
                options=[i for i in options if i != target], prefix=base, complete=complete)


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
