"""Shared topology with per-target paths and coordinated entry switches."""
import copy
import json

import database
import errors
import node_addresses
import node_sync
import topology
import validation
import target_config


def canvas(c, ident):
    return c.execute('SELECT * FROM topology_canvases WHERE root_id=?', (ident,)).fetchone()


def members(c, root):
    return c.execute("SELECT l.*,m.entry,m.target,m.position FROM link_members m JOIN links l ON l.id=m.link_id WHERE m.root_id=? AND l.state<>'deleting' ORDER BY m.position", (root,)).fetchall()


def root_id(c, ident):
    row = c.execute('SELECT root_id FROM link_members WHERE link_id=?', (ident,)).fetchone()
    return row[0] if row else ident


def config_order(c):
    roots = {r['id']: i for i, r in enumerate(c.execute(database.ordered('links')))}
    members_by_id = {r['link_id']: r for r in c.execute('SELECT * FROM link_members')}
    return lambda ident: (roots.get(members_by_id[ident]['root_id'], 0), members_by_id[ident]['position']) if ident in members_by_id else (roots.get(ident, 0), 0)


def config_group(c, link):
    return c.execute('SELECT group_id FROM links WHERE id=?', (root_id(c, link['id']),)).fetchone()[0]


def path_map(graph, values):
    if not isinstance(values, list) or len(values) > 64:
        topology.fail('无效的路径列表', 'path')
    result = {}
    for path in values:
        topology.check_path(graph, path)
        pair = (path[0], path[-1])
        if pair in result: topology.fail('同一入口与目标只能选择一条路径', 'path')
        result[pair] = path
    edges = set(topology.connections(graph))
    for path in list(result.values()):
        for target in (v['id'] for v in graph['vertices'] if v['kind'] == 'target' and not v['disabled']):
            if (path[-2], target) in edges:
                result.setdefault((path[0], target), [*path[:-1], target])
    return result


def preview(c, graph, body):
    import pools
    prefix, required = body.get('prefix', []), body.get('required', [])
    if any(not isinstance(v, list) or any(not isinstance(i, str) for i in v) for v in (prefix, required)):
        topology.fail('无效的路径选择', 'path')
    vertices = {v['id']: v for v in graph['vertices']}
    entry = body.get('entry') or (prefix[0] if prefix else None)
    target = body.get('target')
    if any(value is not None and not isinstance(value, str) for value in (entry, target)) or len(prefix)>64 or len(required)>64:
        topology.fail('无效的路径选择', 'path')
    if entry is not None and vertices.get(entry, {}).get('kind') != 'entry': topology.fail('请选择入口', 'path')
    if target is not None and vertices.get(target, {}).get('kind') != 'target': topology.fail('请选择目标', 'path')
    if any(i not in vertices for i in prefix + required): topology.fail('路径引用的节点不存在', 'path')
    current, active_paths, busy = {}, {}, set()
    if body.get('id'):
        pools.get(c, 'links', body['id'])
        for m in members(c, body['id']):
            path = (pools.runtime_plan(m) or {}).get('graph_path', [])
            current.setdefault(m['entry'], {})[m['target']] = path
            active_paths[m['entry'], m['target']] = path
            if m['job']: busy.add(m['entry'])
    allowed = topology.ready_path(c, graph, None) if body.get('ready', True) else None
    paths, skipped, options, complete = [], [], set(), False
    edges = topology.expanded_edges(graph)
    targets = [v['id'] for v in graph['vertices'] if v['kind'] == 'target']
    for a in (v['id'] for v in graph['vertices'] if v['kind'] == 'entry' and (entry is None or v['id'] == entry)):
        if a in busy:
            skipped.append(dict(entry=a, count=len(targets), targets=targets, reason='此入口正在同步，请等待确认'))
            continue
        preferred = current.get(a, {})
        selected = topology.entry_paths(graph, a, prefix=prefix, required=required, preferred=preferred, allowed=allowed)
        if not selected:
            skipped.append(dict(entry=a, count=len(targets), targets=targets, reason='没有符合选择的可用路径'))
            continue
        choices = set()
        if prefix:
            for source, nxt in edges:
                if source == prefix[-1] and vertices[nxt]['kind'] != 'target' and topology.entry_paths(graph, a, prefix=prefix+[nxt], required=required, preferred=preferred, allowed=allowed):
                    choices.add(nxt)
            if vertices[prefix[-1]]['kind'] == 'target':
                complete = True  # Accept full paths from earlier clients.
            elif not choices or body.get('finish') is True:
                # Only the target suffix is automatic. Any remaining choice
                # between intermediate nodes still belongs to the user.
                final = topology.entry_paths(graph, a, prefix=prefix, direct=True,
                    required=required, preferred=preferred, allowed=allowed)
                if final:
                    selected, choices, complete = final, set(), True
        missing = [t for t in targets if t not in {path[-1] for path in selected}]
        if missing:
            skipped.append(dict(entry=a, count=len(missing), targets=missing,
                                reason='部分目标没有符合选择的可用路径，保留原路'))
        options.update(choices)
        for path in selected:
            paths.append(dict(entry=a, target=path[-1], path=path, unchanged=path == active_paths.get((a, path[-1]))))
    return dict(graph=topology.arrange(graph) if body.get('arrange') else graph, paths=paths, skipped=skipped,
                path=paths[0]['path'] if paths else [], prefix=prefix,
                options=[v['id'] for v in graph['vertices'] if v['id'] in options],
                complete=complete)


def switch(c, body, ident, graph, group):
    import pools
    if not group or topology.snapshot(json.loads(group['applied_graph'] or '{}')) != topology.snapshot(graph):
        raise errors.UserError('运行拓扑已变化，请重新选择', status=409)
    requested = path_map(graph, body.get('paths', [body['path']] if body.get('path') else []))
    if not requested: topology.fail('请先选择完整路径', 'path')
    available = {(m['entry'], m['target']): m for m in members(c, ident)}
    changed, skipped, unchanged, cleared = [], [], [], False
    for entry in dict.fromkeys(a for a, _ in requested):
        entry_changed, entry_unchanged, entry_cleared = [], [], False
        try:
            with database.atomic(c):
                if any(m['entry'] == entry and m['job'] for m in available.values()):
                    raise errors.UserError('此入口正在同步，请等待确认', status=409)
                for pair, path in requested.items():
                    if pair[0] != entry: continue
                    member = available.get(pair)
                    if member is None or not member['applied']:
                        topology.fail('配置尚未生效，请先完成拓扑发布', 'path')
                    active = pools.runtime_plan(member)
                    if active.get('graph_path') == path:
                        if member['state']=='error' and member['failure_scope']:
                            c.execute("UPDATE links SET state='active',error='',failures='[]',failure_scope='',revision=revision+1 WHERE id=?", (member['id'],))
                            pools.event(c, member['id'], '保留当前路径，取消失败的选路')
                            entry_cleared = True
                        entry_unchanged.append(member['id']); continue
                    topology.ready_path(c, graph, path)
                    plan = topology.compile_plan(project(graph, *pair), path, active['port'])
                    selected, physical = pools.choose(c, dict(member), plan)
                    c.execute("UPDATE links SET failures='[]',failure_scope='',revision=revision+1 WHERE id=?", (member['id'],))
                    pools.begin(c, dict(member), plan, selected, physical, '入口路径切换', update_plan=False,
                                target_snapshot=json.loads(member['applied'])['target'], defer_same=True)
                    entry_changed.append(member['id'])
            changed.extend(entry_changed)
            unchanged.extend(entry_unchanged)
            cleared = cleared or entry_cleared
        except errors.UserError as exc:
            skipped.append(dict(entry=entry, count=sum(a == entry for a, _ in requested), reason=str(exc)))
    if changed:
        batch = c.execute('SELECT members FROM link_batches WHERE root_id=?', (ident,)).fetchone()
        ids = list(dict.fromkeys((json.loads(batch['members']) if batch else []) + changed))
        c.execute("INSERT INTO link_batches(root_id,members,graph,path,kind) VALUES (?,?,?,'[]','switch') ON CONFLICT(root_id) DO UPDATE SET members=excluded.members",
                  (ident, json.dumps(ids), json.dumps(graph)))
    if changed or cleared:
        c.execute('UPDATE links SET revision=revision+1 WHERE id=?', (ident,))
    return dict(id=ident, changed=len(changed), unchanged=len(unchanged), skipped=skipped)


def project(graph, entry, target):
    g = copy.deepcopy(topology.flatten(graph))
    g['vertices'] = [v for v in g['vertices'] if v['kind'] not in ('entry', 'target') or v['id'] in (entry, target)]
    vertices = {v['id'] for v in g['vertices']}
    ids = vertices
    g['edges'] = [e for e in g['edges'] if set(e) <= ids]
    # A complete canvas can contain branches belonging to other endpoints.
    # Only keep components on this entry-to-target corridor; the full canvas
    # was validated before projection and remains the editor's source of truth.
    forward, reverse = {i: [] for i in ids}, {i: [] for i in ids}
    for a, b in g['edges']:
        forward[a].append(b)
        reverse[b].append(a)
    def reachable(start, adjacency):
        found, queue = {start}, [start]
        for vertex in queue:
            for neighbour in adjacency[vertex]:
                if neighbour not in found:
                    found.add(neighbour)
                    queue.append(neighbour)
        return found
    corridor = reachable(entry, forward) & reachable(target, reverse)
    corridor.update((entry, target))
    g['vertices'] = [v for v in g['vertices'] if v['id'] in corridor]
    g['edges'] = [e for e in g['edges'] if set(e) <= corridor]
    vertices = {v['id'] for v in g['vertices']}
    if g.get('version') == 2:
        # Runtime switches bypass link_save's validation. Keep each projection
        # identical to its validated form, including all address/port metadata.
        g['address_book'] = {vid: addresses for vid, addresses in g.get('address_book', {}).items() if vid in vertices}
        edges = {'/'.join(e) for e in g['edges']}
        g['bindings'] = {key: value for key, value in g.get('bindings', {}).items() if key in edges}
        pair = entry + '/' + target
        g['ports'] = {key: value for key, value in g.get('ports', {}).items() if key == pair}
    return g


def config_name(c, link):
    return dict(link).get('config_name') or f"配置 {link['id']:04d}"


def config_code(ident):
    return f'C{ident:04d}'


def save(c, body, ident=None):
    import pools
    old = pools.get(c, 'links', ident) if ident else None
    if old and (old['revision'] != body.get('revision') or old['state'] == 'deleting'):
        raise errors.UserError('链路已变化，请刷新后重试', status=409)
    batch = c.execute('SELECT * FROM link_batches WHERE root_id=?', (ident,)).fetchone() if old else None
    switching = body.get('apply') is True and body.get('switch_only') is True and batch and batch['kind'] == 'switch'
    if old and (old['job'] or batch) and not switching:
        raise errors.UserError('请等待当前切换完成', status=409)
    if old and c.execute("SELECT 1 FROM link_members m JOIN links l ON l.id=m.link_id WHERE m.root_id=? AND l.state='deleting'", (ident,)).fetchone():
        raise errors.UserError('旧配置正在清理，请等待节点确认', status=409)
    apply = body.get('apply', False)
    if type(apply) is not bool: topology.fail('无效的应用操作')
    graph = topology.validate(c, body.get('graph'), complete=apply)
    name = validation.check_remark(body.get('name', old['name'] if old else ''), 'name', '名称')
    group_id = database.check_group(c, 'links', body.get('group_id', old['group_id'] if old else None))
    if not apply:
        if not ident:
            ident = pools.link_save(c, dict(name=name, group_id=group_id, plan=dict(entry=None, target=None, stages=[]), apply=False))['id']
        else: c.execute('UPDATE links SET revision=revision+1 WHERE id=?', (ident,))
        draft = dict(graph=graph, name=name, group_id=group_id, port=body.get('port'))
        c.execute('INSERT INTO link_graphs(link_id,draft) VALUES (?,?) ON CONFLICT(link_id) DO UPDATE SET draft=excluded.draft', (ident, json.dumps(draft)))
        return dict(id=ident)
    previous_canvas = canvas(c, ident) if ident else None
    if body.get('switch_only'):
        return switch(c, body, ident, graph, previous_canvas)
    requested = path_map(graph, body.get('paths', [body['path']] if body.get('path') else []))
    graph = topology.arrange(graph)
    if not ident:
        ident = pools.link_save(c, dict(name=name, group_id=group_id, plan=dict(entry=None, target=None, stages=[]), apply=False))['id']
    existing = {(r['entry'], r['target']): r for r in members(c, ident)}
    # The first pre-0.5.1 configuration keeps its ID, chain and subscription bindings.
    if not previous_canvas and old:
        prior = pools.runtime_plan(old) or json.loads(old['plan'])
        old_path = prior.get('graph_path') or []
        pair = (old_path[0], old_path[-1]) if old_path else topology.endpoints(graph)
        existing[pair] = old
    entries = [v for v in graph['vertices'] if v['kind'] == 'entry']
    targets = [v for v in graph['vertices'] if v['kind'] == 'target']
    wanted, ids = set(), []
    allocated_root = bool(existing) or bool(c.execute('SELECT 1 FROM link_members WHERE link_id=?', (ident,)).fetchone())
    for entry in entries:
        preferred = {pair[1]: (pools.runtime_plan(m) or {}).get('graph_path', [])
                     for pair, m in existing.items() if pair[0] == entry['id']}
        selected = topology.entry_paths(graph, entry['id'], preferred=preferred, allowed=topology.ready_path(c, graph, None))
        chosen_paths = {path[-1]: path for path in selected}
        for target in targets:
            pair = (entry['id'], target['id']); wanted.add(pair)
            projection = project(graph, *pair)
            member = existing.get(pair)
            chosen = requested.get(pair) or chosen_paths.get(target['id'])
            if not chosen: topology.fail('没有可用的完整路径，请检查节点状态', 'vertex:'+pair[0])
            topology.ready_path(c, projection, chosen)
            if member is None:
                if not allocated_root and not old:
                    lid = ident; allocated_root = True
                else:
                    lid = pools.link_save(c, dict(name=name, group_id=group_id, plan=dict(entry=None, target=None, stages=[]), apply=False))['id']
                member = pools.get(c, 'links', lid)
            lid = member['id']
            selected_port = graph.get('ports', {}).get('/'.join(pair))
            if selected_port is None and pair == topology.endpoints(graph): selected_port = body.get('port')
            plan = topology.compile_plan(projection, chosen, selected_port)
            c.execute('INSERT INTO link_members VALUES (?,?,?,?,?) ON CONFLICT(link_id) DO UPDATE SET position=excluded.position',
                      (lid, ident, *pair, len(ids)))
            c.execute("UPDATE links SET failure_scope='' WHERE id=?", (lid,))
            pools.link_save(c, dict(name=name, group_id=group_id, plan=plan, revision=member['revision'], apply=True), lid, defer_same=True)
            ids.append(lid)
    removed = [member['id'] for pair, member in existing.items() if pair not in wanted]
    c.execute("INSERT INTO topology_canvases(root_id,graph,path) VALUES (?,?,'[]') ON CONFLICT(root_id) DO UPDATE SET graph=excluded.graph,path='[]'",
              (ident, json.dumps(graph)))
    c.execute('UPDATE links SET name=?,group_id=?,revision=revision+1 WHERE id=?', (name, group_id, ident))
    c.execute('INSERT INTO link_batches(root_id,members,graph,path,removed) VALUES (?,?,?,?,?)',
              (ident, json.dumps(ids), json.dumps(graph), '[]', json.dumps(removed)))
    c.execute('INSERT INTO link_graphs(link_id,draft) VALUES (?,NULL) ON CONFLICT(link_id) DO UPDATE SET draft=NULL', (ident,))
    return dict(id=ident)


def progress(c, now):
    import pools
    for batch in c.execute('SELECT * FROM link_batches').fetchall():
        ids = json.loads(batch['members'])
        links = [pools.get(c, 'links', i) for i in ids]
        running = [(l, json.loads(l['job'])) for l in links if l['job']]
        if batch['kind'] == 'switch':
            by_entry = {}
            for link, job in running:
                entry = c.execute('SELECT entry FROM link_members WHERE link_id=?', (link['id'],)).fetchone()[0]
                by_entry.setdefault(entry, []).append((link, job))
            for jobs in by_entry.values():
                wait = sorted({n for _, j in jobs for n in j['wait']})
                failures = [n for n in wait if c.execute("SELECT 1 FROM sync_ops o JOIN nodes n ON n.id=o.node_id WHERE n.id=? AND o.generation=n.desired_generation AND o.result='failed'", (n,)).fetchone()]
                if failures and all(j['phase'] == 'prepare' for _, j in jobs):
                    for link, job in jobs:
                        pools.cancel_complete(c, link, job)
                        c.execute("UPDATE links SET state='error',error='此入口的新路径准备失败，保留原路',failures=?,failure_scope=? WHERE id=?",
                                  (json.dumps(failures), json.dumps(job['plan']), link['id']))
                elif pools.confirmed(c, wait):
                    for link, job in jobs: pools.progress(c, link, now, force=True)
                elif failures or any(now - j['at'] >= pools.WAIT_SECONDS for _, j in jobs):
                    for link, job in jobs:
                        c.execute("UPDATE links SET state='waiting',error='此入口尚未确认，请同步节点或重试' WHERE id=?", (link['id'],))
            jobs = [json.loads(row['job']) for lid in ids if (row := pools.get(c, 'links', lid))['job']]
            if not jobs:
                c.execute('DELETE FROM link_batches WHERE root_id=?', (batch['root_id'],))
            else:
                phase = 'rollback' if any(j['phase']=='rollback' for j in jobs) else 'commit' if any(j['phase']=='commit' for j in jobs) else 'prepare'
                c.execute('UPDATE link_batches SET phase=? WHERE root_id=?', (phase, batch['root_id']))
            continue
        wait = sorted({n for _, j in running for n in j['wait']})
        failures = [n for n in wait if c.execute("SELECT 1 FROM sync_ops o JOIN nodes n ON n.id=o.node_id WHERE n.id=? AND o.generation=n.desired_generation AND o.result='failed'", (n,)).fetchone()]
        if batch['phase'] == 'prepare' and failures:
            state = c.execute('SELECT blocked FROM link_graphs WHERE link_id=?', (batch['root_id'],)).fetchone()
            blocked = json.loads(state[0]) if state else {}
            blocked.update({str(n):now+120 for n in failures})
            c.execute('UPDATE link_graphs SET blocked=? WHERE link_id=?', (json.dumps(blocked),batch['root_id']))
            for link, job in running:
                pools.cancel_complete(c, link, job)
                c.execute("UPDATE links SET state='error',error='拓扑发布准备失败，入口未切换' WHERE id=?", (link['id'],))
            c.execute('DELETE FROM link_batches WHERE root_id=?', (batch['root_id'],))
            continue
        if not pools.confirmed(c, wait):
            for link, job in running:
                if failures or now - job['at'] >= pools.WAIT_SECONDS:
                    c.execute("UPDATE links SET state='waiting',error='部分配置尚未确认，请同步节点或重试' WHERE id=?", (link['id'],))
            continue
        for link, job in running: pools.progress(c, link, now, force=True)
        still_running = any(c.execute('SELECT job FROM links WHERE id=?', (i,)).fetchone()[0] for i in ids)
        if still_running:
            c.execute("UPDATE link_batches SET phase='commit' WHERE root_id=?", (batch['root_id'],))
        else:
            if batch['phase'] != 'rollback':
                c.execute('UPDATE topology_canvases SET applied_graph=?,applied_path=? WHERE root_id=?',
                          (batch['graph'], batch['path'], batch['root_id']))
                # Obsolete ingress rules and subscription bindings stay usable
                # until every replacement configuration has been acknowledged.
                for lid in json.loads(batch['removed']):
                    c.execute('DELETE FROM subscription_links WHERE link_id=?', (lid,))
                    pools.delete_link(c, lid)
            c.execute('DELETE FROM link_batches WHERE root_id=?', (batch['root_id'],))


def pending(group):
    return topology.snapshot(json.loads(group['graph'])) != topology.snapshot(json.loads(group['applied_graph'] or '{}'))


def decorate(c, link, result, cache=None):
    group = canvas(c, link['id'])
    if not group: return
    import pools
    cache = {} if cache is None else cache
    result.update(graph=json.loads(group['graph']), selection_mode='independent',
                  active_graph=json.loads(group['applied_graph']) if group['applied_graph'] else None,
                  active_path=[])
    configs, pending_nodes = [], {}
    retry = pending = False
    for member in members(c, link['id']):
        state, error = pools.display_state(c, member, cache)
        sync = pools.sync_status(c, member, cache, state)
        pending_nodes.update({n['id']:n for n in sync['nodes']})
        retry = retry or sync['retry']
        pending = pending or pools.pending_plan(member, c)
        applied = pools.runtime_plan(member)
        job = json.loads(member['job']) if member['job'] else None
        failed = json.loads(member['failure_scope']) if member['state']=='error' and member['failure_scope'] else None
        connection = None
        if applied and applied.get('port') and applied.get('ingress_host'):
            connection = target_config.connection(pools.applied_target(c, member), applied['ingress_host'], applied['port'],
                config_name(c, member))
        configs.append(dict(id=member['id'], entry=member['entry'], target=member['target'],
                            name=config_name(c, member), code=config_code(member['id']), state=state, error=error,
                            switching=bool(job),
                            active_path=(applied or {}).get('graph_path', []),
                            pending_path=(job or {}).get('plan', {}).get('graph_path', []),
                            failed_path=(failed or {}).get('graph_path', []),
                            port=(applied or {}).get('port'), connection=connection))
    result['configurations'] = configs
    # Compatibility for single-path consumers; the canvas reads configurations.
    result['active_path'] = next((x['active_path'] for x in configs if x['active_path']), [])
    result['graph_path'] = result['active_path']
    batch = c.execute('SELECT * FROM link_batches WHERE root_id=?', (link['id'],)).fetchone()
    if batch:
        result['state'] = 'waiting' if any(x['state'] == 'waiting' for x in configs) else 'preparing'
        result['error'] = next((x['error'] for x in configs if x['error']), '')
        result['job'] = dict(phase=batch['phase'], kind=batch['kind'])
    elif configs:
        problem = next((x for x in configs if x['state'] != 'active'), None)
        result['state'] = problem['state'] if problem else 'active'
        result['error'] = problem['error'] if problem else ''
    result['pending_changes'] = pending or topology.snapshot(result['graph']) != topology.snapshot(result['active_graph'])
    result['sync'] = dict(nodes=list(pending_nodes.values()), retry=retry, state=result['state'])
    result['events'] = [dict(r) for r in c.execute('''SELECT e.ts,e.message FROM link_events e
        JOIN link_members m ON m.link_id=e.link_id WHERE m.root_id=? ORDER BY e.id DESC LIMIT 30''', (link['id'],))]


def delete(c, ident):
    import pools
    if c.execute('SELECT 1 FROM link_batches WHERE root_id=?', (ident,)).fetchone():
        raise errors.UserError('请等待当前切换完成', status=409)
    group = canvas(c, ident)
    if not group: return pools.delete_link(c, ident)
    ids = [r[0] for r in c.execute('SELECT link_id FROM link_members WHERE root_id=?', (ident,))]
    c.execute('DELETE FROM topology_canvases WHERE root_id=?', (ident,))
    for lid in dict.fromkeys([*ids, ident]):
        if pools.get(c, 'links', lid)['state'] != 'deleting': pools.delete_link(c, lid)
    return dict(deleted=False)


def action(c, body, ident):
    import pools
    root = pools.get(c, 'links', ident)
    if body.get('revision') != root['revision']: raise errors.UserError('链路已变化，请刷新', status=409)
    batch = c.execute('SELECT * FROM link_batches WHERE root_id=?', (ident,)).fetchone()
    if batch is None or batch['kind'] == 'switch':
        if body.get('action') == 'retry':
            retried, skipped = [], []
            resync_nodes, retried_nodes = set(), set()
            failed_entries = {}
            for member in members(c, ident):
                try:
                    with database.atomic(c):
                        sync = pools.sync_status(c, member)
                        if member['job'] and sync['retry']:
                            pools.action(c, dict(action='retry', revision=member['revision']), member['id'],
                                         retried_nodes=retried_nodes)
                        elif member['state']=='error' and member['failure_scope'] and not member['job']:
                            plan = json.loads(member['failure_scope'])
                            failed_entries.setdefault(member['entry'], plan['graph_path'])
                            continue
                        elif not member['job'] and member['applied'] and sync['retry'] and sync['nodes']:
                            resync_nodes.update(n['id'] for n in sync['nodes'])
                            pools.event(c, member['id'], '手动重试节点同步')
                            continue
                        else: continue
                    retried.append(member['id'])
                except errors.UserError as exc:
                    skipped.append(dict(entry=member['entry'],target=member['target'],reason=str(exc)))
            node_sync.retry_nodes(c, resync_nodes - retried_nodes)
            count = len(retried)
            if failed_entries:
                group = canvas(c, ident)
                for entry, path in failed_entries.items():
                    try:
                        result = switch(c, dict(path=path), ident, json.loads(group['applied_graph']), group)
                        count += result['changed']
                        skipped.extend(result['skipped'])
                    except errors.UserError as exc:
                        skipped.append(dict(entry=entry, reason=str(exc)))
            if count:
                c.execute('UPDATE links SET revision=revision+1 WHERE id=?', (ident,))
                return dict(id=ident, retried=count, resynced=len(resync_nodes), skipped=skipped)
            if resync_nodes:
                c.execute('UPDATE links SET revision=revision+1 WHERE id=?', (ident,))
                return dict(id=ident, retried=0, resynced=len(resync_nodes), skipped=skipped)
            if skipped: return dict(id=ident, retried=0, skipped=skipped)
            if batch: return dict(id=ident, retried=0, skipped=[])
        elif body.get('action') == 'cancel' and batch:
            for member in members(c, ident):
                if member['job'] and json.loads(member['job'])['phase'] != 'rollback':
                    pools.action(c, dict(action='cancel', revision=member['revision']), member['id'])
            c.execute("UPDATE link_batches SET phase='rollback' WHERE root_id=?", (ident,))
            c.execute('UPDATE links SET revision=revision+1 WHERE id=?', (ident,))
            return dict(id=ident)
    if not batch:
        if body.get('action') != 'retry': raise errors.UserError('没有待取消的切换')
        group = canvas(c, ident)
        if not pending(group): return dict(id=ident)
        return save(c, dict(graph=json.loads(group['graph']),
                            revision=root['revision'], name=root['name'], group_id=root['group_id'], apply=True), ident)
    for lid in json.loads(batch['members']):
        link = pools.get(c, 'links', lid)
        if link['job']: pools.action(c, dict(action=body.get('action'), revision=link['revision']), lid)
    if body.get('action') == 'cancel':
        if batch['phase'] == 'prepare': c.execute('DELETE FROM link_batches WHERE root_id=?', (ident,))
        else: c.execute("UPDATE link_batches SET phase='rollback' WHERE root_id=?", (ident,))
    return dict(id=ident)
