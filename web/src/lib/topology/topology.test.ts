import { describe, expect, it } from 'vitest';
import { AUTO, type Graph, type PaletteNode, type PaletteTarget, type Vertex } from '$lib/types';
import { byCodePoint, entryStyles, hashId, PALETTE } from './colors';
import { addVertex, connect, freePosition, insertDirect, receiveAddress, removeEdge, removeVertex, replaceResource, setEdgeAddress, setPort } from './edit';
import { createHistory, HISTORY_LIMIT, record, redo, undo } from './history';
import { addressRow, boxesOf, cardHeight, catalogOf, emptyGraph, layerLabels, portAddress, portPoint, worldOf, type Box } from './model';
import { pathBlocked, roundedPath, routeConnections, routeSpans, routeWires, simplify } from './routing';
import { hasCycle, linkError, nameError, validate, validPort, validWeight } from './rules';
import { geometrySignature, stableBy } from './stable';
import { changedHops, entriesThrough, flowingHops, pathSummary, routesOf, spansOf, usedPorts } from './runtime';

const address = (id: string, host: string) => ({ id, primary: id === 'main', name: '', host, local: host, device: 'eth0', gateway: '' });
const node = (id: number, name: string, ids = ['main'], status: PaletteNode['status'] = 'synced'): PaletteNode => ({
  id, name, addr: `10.0.0.${id}`, weight: 0, group_id: 1, tags: [], mode: 'active', status,
  addresses: ids.map((a, i) => address(a, `10.0.${i}.${id}`)),
  network: { addresses: [], sampled_at: null, complete: true },
});
const target = (id: number, name: string): PaletteTarget => ({
  id, name, type: 'endpoint', host: `t${id}.example`, port: 443, params: '{}', group_id: 1, sort: id,
  addresses: [{ id: 'main', name: '默认', host: `t${id}.example`, port: 443 }],
});
const cat = catalogOf({ nodes: [node(1, '入口甲'), node(2, '中转乙'), node(3, '双 IP', ['main', 'alt']), node(4, '删除中', ['main'], 'deleting')], targets: [target(1, '目标')] });
const v = (id: string, kind: Vertex['kind'], extra: Partial<Vertex> = {}): Vertex => ({ id, kind, x: 40, y: 40, disabled: false, ...extra });
function graph(vertices: Vertex[], edges: [string, string][] = []): Graph {
  const g = { ...emptyGraph(), vertices, edges };
  for (const [a, b] of edges) {
    const from = vertices.find((x) => x.id === a)!;
    const to = vertices.find((x) => x.id === b)!;
    g.bindings[`${a}/${b}`] = { from: from.kind === 'direct' ? {} : { [a]: AUTO }, to: to.kind === 'direct' ? {} : { [b]: 'main' } };
  }
  return g;
}
const base = () =>
  graph(
    [v('e', 'entry', { node_id: 1, address: 'main' }), v('n', 'node', { node_id: 2, x: 440 }), v('m', 'node', { node_id: 3, x: 440, y: 300 }), v('d', 'direct', { weight: 0, x: 300, y: 300 }), v('t', 'target', { target_id: 1, x: 840 })],
    [['e', 'n'], ['n', 't'], ['e', 'd'], ['d', 'm'], ['m', 't']],
  );

describe('model', () => {
  it('sizes cards like the server layout', () => {
    expect(cardHeight(v('d', 'direct'), 0)).toBe(56);
    expect(cardHeight(v('n', 'node', { node_id: 3 }), 2)).toBe(62 + 24 * 3);
    expect(cardHeight(v('t', 'target', { target_id: 1 }), 0)).toBe(62 + 24);
    const boxes = boxesOf(base(), cat);
    expect(boxes.get('m')).toMatchObject({ width: 220, height: 134 });
    expect(boxes.get('d')).toMatchObject({ width: 110, height: 56 });
  });
  it('anchors ports on address rows, auto egress below them', () => {
    const g = base();
    const m = g.vertices[2];
    const box = boxesOf(g, cat).get('m')!;
    const rows = cat.nodes.get(3)!.addresses;
    expect(portPoint(box, m, rows, 'in', 'alt')).toEqual([440, 300 + 62 + 24 + 12]);
    expect(portPoint(box, m, rows, 'out', AUTO)).toEqual([660, 300 + 62 + 48 + 12]);
    expect(addressRow(rows, 'missing')).toBe(0);
    expect(portPoint(boxesOf(g, cat).get('d')!, g.vertices[3], [], 'out', null)).toEqual([410, 328]);
  });
  it('resolves the address a port shows', () => {
    const g = base();
    expect(portAddress(g, g.vertices[0], 'in', null, cat)).toBe('main');
    expect(portAddress(g, g.vertices[2], 'in', ['d', 'm'], cat)).toBe('main');
    expect(portAddress(g, g.vertices[1], 'out', null, cat)).toBe(AUTO);
    const lone = graph([v('x', 'node', { node_id: 3 }), v('y', 'node', { node_id: 2 })]);
    expect(portAddress(lone, lone.vertices[0], 'in', null, cat)).toBe('');
    expect(portAddress(lone, lone.vertices[1], 'in', null, cat)).toBe('main');
    expect(portAddress(lone, { ...lone.vertices[0], in_address: 'alt' }, 'in', null, cat)).toBe('alt');
  });
  it('computes the world box and layer captions', () => {
    expect(worldOf([])).toEqual({ x: 0, y: 0, width: 700, height: 320 });
    expect(worldOf([{ id: 'a', x: 40, y: 60, width: 220, height: 110 }])).toEqual({ x: -20, y: -40, width: 700, height: 320 });
    const g = graph([v('e', 'entry', { layer: 0, y: 90 }), v('n', 'node', { layer: 1, x: 440 }), v('t', 'target', { layer: 2, x: 840 }), v('d', 'direct', { layer: null })]);
    expect(layerLabels(g).map((l) => l.text)).toEqual(['入口', '第 1 层', '目标']);
    expect(layerLabels(g)[0]).toMatchObject({ x: 40, y: 55 });
  });
});

describe('linkError', () => {
  const g = base();
  it.each([
    ['e', 'zz', '连接的节点已不存在'],
    ['n', 'n', '不能连接组件自身'],
    ['t', 'n', '入口只能向外连接，目标只能接收连接'],
    ['n', 'e', '入口只能向外连接，目标只能接收连接'],
    ['e', 'n', '这条连线已存在'],
    ['m', 'd', 'direct 只能连入和连出各一条线'],
  ])('%s -> %s: %s', (a, b, message) => expect(linkError(g, a, b, cat)).toBe(message));
  it('rejects the same machine on both ends, also across a direct', () => {
    const same = graph([v('a', 'entry', { node_id: 2 }), v('b', 'node', { node_id: 2 }), v('d', 'direct'), v('c', 'node', { node_id: 2 })], [['a', 'd']]);
    expect(linkError(same, 'a', 'b', cat)).toBe('两端都是同一台节点「中转乙」，不能互相转发');
    expect(linkError(same, 'd', 'c', cat)).toBe('direct 两端不能是同一台节点');
  });
  it('rejects consecutive directs and cycles', () => {
    const two = graph([v('d1', 'direct'), v('d2', 'direct')]);
    expect(linkError(two, 'd1', 'd2', cat)).toBe('direct 不能连续连接');
    const chain = graph([v('a', 'node', { node_id: 1 }), v('b', 'node', { node_id: 2 }), v('c', 'node', { node_id: 3 })], [['a', 'b'], ['b', 'c']]);
    expect(linkError(chain, 'c', 'a', cat)).toBe('这条连线会形成循环');
    expect(linkError(chain, 'a', 'c', cat)).toBeNull();
  });
  it('allows entry -> target and entry -> direct -> target', () => {
    const g2 = graph([v('e', 'entry', { node_id: 1 }), v('d', 'direct'), v('t', 'target', { target_id: 1 })], [['e', 'd']]);
    expect(linkError(g2, 'e', 't', cat)).toBeNull();
    expect(linkError(g2, 'd', 't', cat)).toBeNull();
  });
});

describe('validate', () => {
  const messages = (g: Graph, level = 'error') => validate(g, cat).filter((i) => i.level === level).map((i) => i.message);
  it('accepts a complete graph', () => expect(validate(base(), cat)).toEqual([]));
  it('requires an entry and a target', () => expect(messages(emptyGraph())).toEqual(['请至少添加一个入口', '请至少添加一个目标']));
  it('reports dangling and over-wired directs', () => {
    const g = base();
    expect(messages(removeEdge(g, ['d', 'm']))).toContain('请完成 direct 两端的连接');
    const forced = { ...g, edges: [...g.edges, ['n', 'd'] as [string, string]] };
    expect(messages(forced)).toContain('direct 只能连接一个上游和一个下游');
  });
  it('reports a missing receive IP on multi-address nodes and entries', () => {
    const g = base();
    const open = setEdgeAddress(g, ['d', 'm'], 'in', null);
    expect(validate(open, cat).find((i) => i.message === '请为连线选择接收 IP')?.edge).toEqual(['d', 'm']);
    const entry = graph([v('e', 'entry', { node_id: 3 }), v('t', 'target', { target_id: 1 })], [['e', 't']]);
    expect(validate(entry, cat)[0]).toMatchObject({ message: '请选择「双 IP」的接收 IP', vertex: 'e' });
  });
  it('reports same machine, deleting nodes, missing resources, bad weights and ports', () => {
    const g = replaceResource(base(), cat, 'n', 1);
    expect(validate(g, cat).find((i) => i.message === '不能将同一台机器连接到自身')).toMatchObject({ vertex: 'n', edge: ['e', 'n'] });
    expect(messages(replaceResource(base(), cat, 'n', 4))).toContain('节点「删除中」正在删除');
    expect(messages(replaceResource(base(), cat, 'n', 99))).toContain('节点不存在，请更换或删除');
    const g2 = base();
    g2.vertices[3] = { ...g2.vertices[3], weight: 1.5 };
    expect(messages(g2)).toContain('权重须为 -100000 至 100000 的整数');
    expect(messages(setPort(base(), 'e', 't', 70000))).toContain('端口必须是 1–65535 的整数');
  });
  it('detects cycles and warns about vertices on no entry-target walk', () => {
    const g = base();
    const cyc = { ...g, edges: [...g.edges, ['m', 'n'] as [string, string], ['n', 'm'] as [string, string]] };
    expect(hasCycle(cyc)).toBe(true);
    expect(messages(cyc)).toContain('拓扑存在循环连接');
    expect(hasCycle(g)).toBe(false);
    const stray = addVertex(g, cat, { id: 'x', kind: 'node', resource: 2, x: 0, y: 500 });
    expect(validate(stray, cat)).toEqual([{ level: 'warn', message: '当前没有可达的入口—目标配置', vertex: 'x' }]);
  });
  it('checks names, weights and ports', () => {
    expect(nameError('  ')).toBeTruthy();
    expect(nameError('x'.repeat(65))).toBeTruthy();
    expect(nameError('线路 A')).toBeNull();
    expect([validWeight(-100000), validWeight(100001), validWeight(1.5), validWeight(null)]).toEqual([true, false, false, false]);
    expect([validPort(1), validPort(65535), validPort(0), validPort(80.5)]).toEqual([true, true, false, false]);
  });
});

describe('edit', () => {
  it('adds vertices with defaults and avoids covering cards', () => {
    let g = addVertex(emptyGraph(), cat, { id: 'e', kind: 'entry', resource: 1, x: 100, y: 100 });
    expect(g.vertices[0]).toMatchObject({ node_id: 1, out_address: AUTO, in_address: 'main', address: 'main', x: 100, y: 100 });
    g = addVertex(g, cat, { id: 'm', kind: 'node', resource: 3, x: 210, y: 155, centre: true });
    expect(g.vertices[1].in_address).toBeUndefined();
    expect(g.vertices[1].x).toBe(100 + 220 + 40);
    expect(addVertex(g, cat, { id: 'd', kind: 'direct', x: 0, y: 0 }).vertices[2]).toMatchObject({ weight: 0 });
    const boxes: Box[] = [{ id: 'a', x: 0, y: 0, width: 220, height: 110 }, { id: 'b', x: 260, y: 0, width: 220, height: 110 }];
    expect(freePosition(boxes, 10, 10, 220, 110)).toEqual({ x: 520, y: 10 });
  });
  it('connects with bindings and remembers the choices on the vertices', () => {
    const g = graph([v('e', 'entry', { node_id: 1 }), v('m', 'node', { node_id: 3 }), v('d', 'direct')]);
    expect(receiveAddress(g, 'm', cat)).toBeNull();
    expect(receiveAddress(g, 'm', cat, 'alt')).toBe('alt');
    expect(receiveAddress(g, 'e', cat, 'nope')).toBe('main');
    const two = catalogOf({ nodes: [], targets: [{ ...target(1, '目标'), addresses: [...target(1, '目标').addresses, { id: 'bk', name: '备用', host: 'bk', port: 1 }] }] });
    const tg = graph([v('t', 'target', { target_id: 1 })]);
    expect(receiveAddress(tg, 't', two)).toBe('main');
    expect(receiveAddress(tg, 't', two, 'bk')).toBe('bk');
    expect(connect(g, cat, 'e', 'm', AUTO, null).error).toBe('请选择「双 IP」的接收 IP');
    const done = connect(g, cat, 'e', 'm', 'main', 'alt').graph!;
    expect(done.bindings['e/m']).toEqual({ from: { e: 'main' }, to: { m: 'alt' } });
    expect(done.vertices[0].out_address).toBe('main');
    expect(done.vertices[1].in_address).toBe('alt');
    expect(connect(done, cat, 'e', 'd', AUTO, null).graph!.bindings['e/d']).toEqual({ from: { e: AUTO }, to: {} });
    expect(connect(done, cat, 'e', 'm', AUTO, 'alt').error).toBe('这条连线已存在');
    expect(g.edges).toEqual([]);
  });
  it('changes one edge end without touching the others', () => {
    const g = setEdgeAddress(base(), ['m', 't'], 'out', 'alt');
    expect(g.bindings['m/t']).toEqual({ from: { m: 'alt' }, to: { t: 'main' } });
    expect(g.bindings['n/t'].from).toEqual({ n: AUTO });
  });
  it('inserts a direct in place and removes it again', () => {
    const g = base();
    const split = insertDirect(g, cat, ['e', 'n'], 'x').graph!;
    expect(split.edges.slice(0, 2)).toEqual([['e', 'x'], ['x', 'n']]);
    expect(split.bindings['e/x']).toEqual({ from: { e: AUTO }, to: {} });
    expect(split.bindings['x/n']).toEqual({ from: {}, to: { n: 'main' } });
    expect(split.bindings['e/n']).toBeUndefined();
    expect(split.vertices.at(-1)).toMatchObject({ kind: 'direct', weight: 0, x: (40 + 220 + 440 - 110) / 2 });
    expect(insertDirect(split, cat, ['e', 'x'], 'y').error).toBe('direct 不能连续连接');
    const back = removeVertex(split, 'x').graph!;
    expect(back.edges).toEqual(g.edges);
    expect(back.bindings).toEqual(g.bindings);
    expect(validate(split, cat)).toEqual([]);
  });
  it('refuses to dissolve a direct next to an existing wire', () => {
    const g = base();
    const both = connect(g, cat, 'e', 'm', AUTO, 'alt').graph!;
    expect(removeVertex(both, 'd').error).toBe('两端已有另一条直连，请先删除其中一条连线');
  });
  it('removes a vertex with its wires, bindings and ports', () => {
    const g = removeVertex(setPort(base(), 'e', 't', 8080), 'n').graph!;
    expect(g.edges).toEqual([['e', 'd'], ['d', 'm'], ['m', 't']]);
    expect(Object.keys(g.bindings).sort()).toEqual(['d/m', 'e/d', 'm/t']);
    expect(removeVertex(setPort(base(), 'e', 't', 8080), 't').graph!.ports).toEqual({});
    expect(setPort(setPort(base(), 'e', 't', 8080), 'e', 't', null).ports).toEqual({});
  });
  it('replaces a resource and re-defaults its addresses', () => {
    const g = replaceResource(setEdgeAddress(base(), ['d', 'm'], 'in', 'alt'), cat, 'm', 2);
    expect(g.vertices[2]).toMatchObject({ node_id: 2, in_address: 'main', out_address: AUTO });
    expect(g.bindings['d/m'].to).toEqual({ m: 'main' });
    const multi = replaceResource(base(), cat, 'n', 3);
    expect(multi.bindings['e/n'].to).toEqual({});
    expect(multi.vertices[1].in_address).toBeUndefined();
  });
});

describe('history', () => {
  it('records, undoes and redoes; equal states are not recorded', () => {
    let h = createHistory({ n: 0 });
    h = record(h, { n: 0 });
    expect(h.past).toHaveLength(0);
    h = record(record(h, { n: 1 }), { n: 2 });
    h = undo(h);
    expect(h.present).toEqual({ n: 1 });
    expect(redo(h).present).toEqual({ n: 2 });
    h = record(h, { n: 5 });
    expect(h.future).toEqual([]);
    expect(undo(undo(undo(h))).present).toEqual({ n: 0 });
    expect(redo(redo(h))).toBe(redo(h));
  });
  it('keeps the newest 50 steps', () => {
    let h = createHistory(0);
    for (let i = 1; i <= 60; i++) h = record(h, i);
    expect(h.past).toHaveLength(HISTORY_LIMIT);
    expect(h.past[0]).toBe(10);
  });
});

describe('routing', () => {
  const box = (id: string, x: number, y: number, width = 220, height = 110): Box => ({ id, x, y, width, height });
  it('rounds corners and merges collinear points', () => {
    expect(simplify([[0, 0], [0, 0], [10, 0], [20, 0], [20, 10]])).toEqual([[0, 0], [20, 0], [20, 10]]);
    expect(roundedPath([[0, 0], [20, 0], [20, 30]])).toBe('M 0 0 L 12 0 Q 20 0 20 8 L 20 30');
    expect(roundedPath([[0, 0], [6, 0], [6, 30]])).toBe('M 0 0 L 3 0 Q 6 0 6 3 L 6 30');
  });
  it('detects blocked segments with padding; touching is fine', () => {
    const b = [box('a', 100, 100, 100, 100)];
    expect(pathBlocked([[0, 150], [300, 150]], b)).toBe(true);
    expect(pathBlocked([[0, 88], [300, 88]], b)).toBe(false);
    expect(pathBlocked([[0, 89], [300, 89]], b)).toBe(true);
    expect(pathBlocked([[0, 0], [50, 0], [50, 300]], b)).toBe(false);
  });
  const wire = (a: Box, b: Box, dy = 50, ey = 50) => ({ a: a.id, b: b.id, start: [a.x + a.width, a.y + dy] as [number, number], end: [b.x, b.y + ey] as [number, number] });
  const orthogonal = (p: [number, number][]) => p.every((q, i) => !i || q[0] === p[i - 1][0] || q[1] === p[i - 1][1]);
  const forward = (p: [number, number][]) => p.every((q, i) => !i || q[0] >= p[i - 1][0]);
  it('routes straight, then with one bend in the channel', () => {
    const a = box('a', 0, 0);
    const b = box('b', 400, 0);
    const c = box('c', 400, 200);
    const [straight, bent] = routeWires([wire(a, b), wire(a, c, 60)], [a, b, c]);
    expect(straight).toEqual([[220, 50], [400, 50]]);
    expect(bent).toEqual([[220, 60], [310, 60], [310, 250], [400, 250]]);
  });
  it('passes columns in a straight lane beside the cards, never backwards', () => {
    const a = box('a', 0, 100);
    const wall = [box('w1', 400, 0), box('w2', 400, 160), box('w3', 400, 320)];
    const b = box('b', 800, 100);
    const [p] = routeWires([wire(a, b)], [a, ...wall, b]);
    expect(orthogonal(p) && forward(p)).toBe(true);
    expect(pathBlocked(p, wall, 12)).toBe(false);
    // Through the gap between w1 and w2, not around the whole column.
    expect(Math.max(...p.map((q) => q[1]))).toBeLessThan(160);
    expect(Math.min(...p.map((q) => q[1]))).toBeGreaterThan(110);
  });
  it('orders the bends of one channel so parallel wires do not cross', () => {
    const a = box('a', 0, 0);
    const b = box('b', 400, 300);
    const [upper, lower] = routeWires([wire(a, b, 40, 40), wire(a, b, 50, 50)], [a, b]);
    // Going down, the upper wire turns further right.
    expect(upper[1][0]).toBeGreaterThan(lower[1][0]);
    const up = routeWires([wire(b, box('c', 800, 0), 40, 40), wire(b, box('c', 800, 0), 50, 50)], [b, box('c', 800, 0)]);
    expect(up[0][1][0]).toBeLessThan(up[1][1][0]);
  });
  it('shares one trunk between wires leaving the same point and reuses lanes that never meet', () => {
    const a = box('a', 0, 200);
    const b = box('b', 400, 0);
    const c = box('c', 400, 400);
    const [up, down] = routeWires([wire(a, b), wire(a, c)], [a, b, c]);
    expect(up[1][0]).toBe(down[1][0]);
    const d = box('d', 0, 600);
    const e = box('e', 400, 800);
    const [first, second] = routeWires([wire(a, b), wire(d, e)], [a, b, d, e]);
    expect(first[1][0]).toBe(310);
    expect(second[1][0]).toBe(310);
  });
  it('loops around when the target is not in a later column', () => {
    const a = box('a', 600, 0);
    const b = box('b', 0, 300);
    const [p] = routeWires([wire(a, b)], [a, b]);
    expect(orthogonal(p)).toBe(true);
    expect(pathBlocked(p.slice(1, -1), [a, b], 3)).toBe(false);
    expect(p[0]).toEqual([820, 50]);
    expect(p.at(-1)).toEqual([0, 350]);
  });
  it('fans out wires sharing a connector in the order of the far card', () => {
    const g = base();
    const boxes = boxesOf(g, cat);
    const routes = routeConnections(g, boxes, cat);
    expect(routes.map((r) => r.key)).toEqual(['e/n', 'n/t', 'e/d', 'd/m', 'm/t']);
    const start = (key: string) => routes.find((r) => r.key === key)!.points[0];
    // Both leave e's auto row; n (y 40) is above d (y 300) so it takes the upper slot.
    expect(start('e/n')[1]).toBeLessThan(start('e/d')[1]);
    expect(start('e/d')[1] - start('e/n')[1]).toBe(9);
    const end = (key: string) => routes.find((r) => r.key === key)!.points.at(-1)!;
    expect(end('n/t')[1]).toBeLessThan(end('m/t')[1]);
    expect(routeConnections(g, boxes, cat, { editing: true })[0].points[0][0]).toBe(40 + 220 + 9);
    for (const r of routes) expect(r.d.startsWith('M ')).toBe(true);
  });
  it('gives each entry its own slot on a shared connector', () => {
    const g = graph([v('e1', 'entry', { node_id: 1 }), v('e2', 'entry', { node_id: 2, y: 300 }), v('m', 'node', { node_id: 3, x: 440 }), v('t', 'target', { target_id: 1, x: 840 })], [['e1', 'm'], ['e2', 'm'], ['m', 't']]);
    const spans = [
      { a: 'e1', b: 'm', entry: 'e1', pending: false, addresses: [AUTO, 'main'] as [string, string] },
      { a: 'm', b: 't', entry: 'e1', pending: false, addresses: [AUTO, 'main'] as [string, string] },
      { a: 'e2', b: 'm', entry: 'e2', pending: false, addresses: [AUTO, 'main'] as [string, string] },
      { a: 'm', b: 't', entry: 'e2', pending: false, addresses: [AUTO, 'main'] as [string, string] },
    ];
    const routes = routeSpans(g, spans, boxesOf(g, cat), cat);
    expect(routes).toHaveLength(4);
    expect(routes[1].points[0][1]).not.toBe(routes[3].points[0][1]);
    expect(routes[0].points.at(-1)![1]).not.toBe(routes[2].points.at(-1)![1]);
    expect(Math.abs(routes[1].points.at(-1)![1] - routes[3].points.at(-1)![1])).toBe(8);
  });
});

describe('colours', () => {
  const entries = (...ids: string[]) => ({ vertices: ids.map((id) => v(id, 'entry')) });
  it('is deterministic and independent of vertex order', () => {
    const a = entryStyles(entries('v1', 'v9', 'abc'));
    const b = entryStyles(entries('abc', 'v9', 'v1'));
    expect([...a]).toEqual([...b]);
    expect([...a.keys()]).toEqual(['abc', 'v1', 'v9']);
    expect([...a.values()].map((s) => s.ordinal)).toEqual([1, 2, 3]);
  });
  it('takes the hashed palette slot and probes forward on collisions', () => {
    expect(hashId('ab')).toBe(97 * 31 + 98);
    const slot = hashId('a') % PALETTE.length;
    expect(entryStyles(entries('a')).get('a')!.color).toBe(PALETTE[slot]);
    // 'a' and 'i' are 8 apart, so they hash to the same slot; the later id moves on.
    const both = entryStyles(entries('i', 'a'));
    expect(both.get('a')!.color).toBe(PALETTE[slot]);
    expect(both.get('i')!.color).toBe(PALETTE[(slot + 1) % PALETTE.length]);
  });
  it('never repeats a colour, also past the palette size', () => {
    const many = entryStyles(entries(...Array.from({ length: 14 }, (_, i) => `entry-${i}`)));
    expect(new Set([...many.values()].map((s) => s.color)).size).toBe(14);
  });
  it('orders ids by code point', () => expect(['b', 'B', 'a', '1'].sort(byCodePoint)).toEqual(['1', 'B', 'a', 'b']));
});

describe('runtime', () => {
  const g = base();
  const config = (id: number, active: string[], pending: string[] = [], state = 'active') =>
    ({ id, entry: 'e', target: 't', state, active_path: active, pending_path: pending, active_graph: g, pending_graph: pending.length ? g : null }) as never;
  const link = { configurations: [config(1, ['e', 'n', 't'], ['e', 'd', 'm', 't'], 'switching')] };
  it('lists applied and pending routes', () => {
    const routes = routesOf(link);
    expect(routes.map((r) => [r.pending, r.path.length])).toEqual([[false, 3], [true, 4]]);
    expect(routesOf({ configurations: [config(2, [])] })).toEqual([]);
  });
  it('builds spans, dropping pending hops equal to live ones', () => {
    const same = { configurations: [config(1, ['e', 'n', 't'], ['e', 'n', 't'])] };
    expect(spansOf(routesOf(same), g, ['e'])).toHaveLength(2);
    const spans = spansOf(routesOf(link), g, ['e']);
    expect(spans.map((s) => `${s.a}>${s.b}${s.pending ? '?' : ''}`)).toEqual(['e>n', 'n>t', 'd>m?', 'e>d?', 'm>t?']);
    expect(spans[0].addresses).toEqual([AUTO, 'main']);
  });
  it('marks healthy applied hops as flowing', () => {
    expect([...flowingHops(routesOf(link), g, cat)]).toEqual(['e|e|n', 'e|n|t']);
    const sick = replaceResource(g, cat, 'n', 4);
    expect(flowingHops(routesOf(link), sick, cat).size).toBe(0);
  });
  it('summarises entries, ports and paths', () => {
    const routes = routesOf(link);
    expect(entriesThrough(routes).get('n')).toEqual(['e']);
    expect(entriesThrough(routes).has('m')).toBe(false);
    expect([...usedPorts(routes, g).get('n')!.in]).toEqual(['main']);
    expect([...usedPorts(routes, g).get('e')!.in]).toEqual(['main']);
    expect(pathSummary(['e', 'n', 't'], g, cat)).toEqual({ text: '中转乙', full: '入口甲 → 中转乙 → 目标' });
    expect(pathSummary(['e', 't'], g, cat).text).toBe('direct');
    expect(pathSummary([], g, cat).text).toBe('等待生效');
  });
  it('draws only the hops a failover would change', () => {
    const row = { entry: 'e', path: ['e', 'n', 't'] } as never;
    expect(changedHops(row, g, routesOf(link), g)).toEqual([]);
    const other = { entry: 'e', path: ['e', 'd', 'm', 't'] } as never;
    expect(changedHops(other, g, routesOf(link), g).map((s) => s.b)).toEqual(['d', 'm', 't']);
  });
});

describe('stable references', () => {
  it('keeps the earlier object until the signature changes', () => {
    const keep = stableBy((r: { nodes: PaletteNode[]; targets: PaletteTarget[] }) => geometrySignature(r));
    const first = { nodes: [node(1, 'a')], targets: [target(1, 't')] };
    expect(keep(first)).toBe(first);
    // A status change does not move anything on the canvas.
    expect(keep({ nodes: [node(1, 'a', ['main'], 'offline')], targets: [target(1, 't')] })).toBe(first);
    const grown = { nodes: [node(1, 'a', ['main', 'alt'])], targets: [target(1, 't')] };
    expect(keep(grown)).toBe(grown);
    const plain = stableBy<number[]>();
    const list = [1, 2];
    expect(plain(list)).toBe(list);
    expect(plain([1, 2])).toBe(list);
  });
});
