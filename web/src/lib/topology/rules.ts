// Client copy of the server's topology rules (server/topology.py validate). The server stays the authority;
// these give immediate feedback while editing.
import { AUTO, type Graph, type Vertex, type VertexId } from '$lib/types';
import { addressesOf, boundAddress, connections, nodeOf, targetOf, vertexName, vertexOf, type Catalog, type Edge } from './model';

export const MAX_VERTICES = 256;
export const MAX_CONFIGURATIONS = 64;
export const WEIGHT_LIMIT = 100000;

export const validWeight = (value: unknown): value is number =>
  typeof value === 'number' && Number.isInteger(value) && Math.abs(value) <= WEIGHT_LIMIT;
export const validPort = (value: unknown): value is number =>
  typeof value === 'number' && Number.isInteger(value) && value >= 1 && value <= 65535;

/** Error text for a link name, or null. */
export function nameError(name: string): string | null {
  const trimmed = name.trim();
  // eslint-disable-next-line no-control-regex
  return !trimmed || trimmed.length > 64 || /[\u0000-\u001f\u007f]/.test(trimmed) ? '名称需要包含 1 至 64 个字符' : null;
}

function successors(g: Graph): Map<VertexId, VertexId[]> {
  const next = new Map<VertexId, VertexId[]>();
  for (const [a, b] of connections(g)) next.set(a, [...(next.get(a) ?? []), b]);
  return next;
}

/** Vertices reachable from `start` along `next` (including the start vertices). */
function reach(start: VertexId[], next: Map<VertexId, VertexId[]>): Set<VertexId> {
  const seen = new Set(start);
  const stack = [...start];
  while (stack.length) for (const b of next.get(stack.pop()!) ?? []) if (!seen.has(b)) (seen.add(b), stack.push(b));
  return seen;
}

/** Why `from -> to` may not be connected, or null when the connection is legal. */
export function linkError(g: Graph, from: VertexId, to: VertexId, cat: Catalog): string | null {
  const a = vertexOf(g, from);
  const b = vertexOf(g, to);
  if (!a || !b) return '连接的节点已不存在';
  if (from === to) return '不能连接组件自身';
  if (a.kind === 'target' || b.kind === 'entry') return '入口只能向外连接，目标只能接收连接';
  if (a.node_id != null && a.node_id === b.node_id) return `两端都是同一台节点「${vertexName(a, cat)}」，不能互相转发`;
  const edges = connections(g);
  if (edges.some((e) => e[0] === from && e[1] === to)) return '这条连线已存在';
  if (a.kind === 'direct' && b.kind === 'direct') return 'direct 不能连续连接';
  if ((a.kind === 'direct' && edges.some((e) => e[0] === from)) || (b.kind === 'direct' && edges.some((e) => e[1] === to)))
    return 'direct 只能连入和连出各一条线';
  // The real machines on both sides of a direct must differ.
  const upstream = a.kind === 'direct' ? vertexOf(g, edges.find((e) => e[1] === from)?.[0]) : a;
  const downstream = b.kind === 'direct' ? vertexOf(g, edges.find((e) => e[0] === to)?.[1]) : b;
  if ((a.kind === 'direct' || b.kind === 'direct') && upstream?.node_id != null && upstream.node_id === downstream?.node_id)
    return 'direct 两端不能是同一台节点';
  if (reach([to], successors(g)).has(from)) return '这条连线会形成循环';
  return null;
}

export interface Issue {
  level: 'error' | 'warn';
  message: string;
  vertex?: VertexId;
  edge?: Edge;
}

/** True when the graph has a directed cycle (Kahn). */
export function hasCycle(g: Graph): boolean {
  const indegree = new Map(g.vertices.map((v) => [v.id, 0]));
  const next = successors(g);
  for (const list of next.values()) for (const b of list) indegree.set(b, (indegree.get(b) ?? 0) + 1);
  const queue = [...indegree].filter(([, n]) => n === 0).map(([id]) => id);
  let visited = 0;
  while (queue.length) {
    const id = queue.pop()!;
    visited++;
    for (const b of next.get(id) ?? []) {
      const n = indegree.get(b)! - 1;
      indegree.set(b, n);
      if (n === 0) queue.push(b);
    }
  }
  return visited < g.vertices.length;
}

/**
 * Everything that would make the server reject a save, plus warnings it would return.
 * Messages match the server texts where one exists.
 */
export function validate(g: Graph, cat: Catalog): Issue[] {
  const issues: Issue[] = [];
  const error = (message: string, at: Pick<Issue, 'vertex' | 'edge'> = {}) => issues.push({ level: 'error', message, ...at });
  const byId = new Map(g.vertices.map((v) => [v.id, v]));
  const edges = connections(g);
  if (g.vertices.length > MAX_VERTICES) error('画布最多包含 256 个节点');
  if (!g.vertices.some((v) => v.kind === 'entry')) error('请至少添加一个入口');
  if (!g.vertices.some((v) => v.kind === 'target')) error('请至少添加一个目标');

  for (const v of g.vertices) {
    const name = vertexName(v, cat);
    if (v.kind === 'direct') {
      if (!validWeight(v.weight)) error('权重须为 -100000 至 100000 的整数', { vertex: v.id });
      const ins = edges.filter((e) => e[1] === v.id);
      const outs = edges.filter((e) => e[0] === v.id);
      if (ins.length > 1 || outs.length > 1) error('direct 只能连接一个上游和一个下游', { vertex: v.id });
      else if (ins.length !== 1 || outs.length !== 1) error('请完成 direct 两端的连接', { vertex: v.id });
      else {
        const a = byId.get(ins[0][0]);
        const b = byId.get(outs[0][1]);
        if (a?.node_id != null && a.node_id === b?.node_id) error('不能通过 direct 连接同一台机器', { vertex: v.id });
      }
      continue;
    }
    if (v.kind === 'target') {
      if (!targetOf(v, cat)) error('目标不存在，请更换或删除', { vertex: v.id });
      continue;
    }
    const node = nodeOf(v, cat);
    if (!node) {
      error('节点不存在，请更换或删除', { vertex: v.id });
      continue;
    }
    if (node.status === 'deleting') error(`节点「${name}」正在删除`, { vertex: v.id });
    if (v.kind === 'entry') {
      const addresses = addressesOf(g, v, cat);
      const chosen = v.address && addresses.some((a) => a.id === v.address);
      if (!chosen && addresses.length !== 1) error(`请选择「${name}」的接收 IP`, { vertex: v.id });
    }
  }

  for (const edge of edges) {
    const [a, b] = edge.map((id) => byId.get(id)) as [Vertex | undefined, Vertex | undefined];
    if (!a || !b) {
      error('连线引用不存在的节点', { edge });
      continue;
    }
    if (a.kind === 'target' || b.kind === 'entry') error('入口不能接收入站连线，目标不能继续转发', { edge });
    if (a.node_id != null && a.node_id === b.node_id) error('不能将同一台机器连接到自身', { edge, vertex: b.id });
    if (a.kind === 'direct' && b.kind === 'direct') error('不能连续连接 direct', { edge });
    if (a.kind !== 'direct') {
      const out = boundAddress(g, edge, 'out');
      if (out && out !== AUTO && !addressesOf(g, a, cat).some((x) => x.id === out)) error('请为连线选择发出 IP', { edge });
    }
    if (b.kind !== 'direct') {
      const addresses = addressesOf(g, b, cat);
      const into = boundAddress(g, edge, 'in');
      const valid = into && addresses.some((x) => x.id === into);
      // The server fills the only address of a single-address node and `main` of a target.
      if (!valid && (into || (b.kind !== 'target' && addresses.length !== 1))) error('请为连线选择接收 IP', { edge });
    }
  }

  for (const [key, port] of Object.entries(g.ports ?? {})) {
    if (!validPort(port)) error('端口必须是 1–65535 的整数', { vertex: key.split('/')[0] });
  }

  if (hasCycle(g)) error('拓扑存在循环连接');
  else {
    // Reachability over real hops; directs only count when fully wired, like the server's folded graph.
    const next = successors(g);
    const previous = new Map<VertexId, VertexId[]>();
    for (const [a, b] of edges) previous.set(b, [...(previous.get(b) ?? []), a]);
    const entries = g.vertices.filter((v) => v.kind === 'entry').map((v) => v.id);
    const targets = g.vertices.filter((v) => v.kind === 'target').map((v) => v.id);
    const fromEntry = reach(entries, next);
    const toTarget = reach(targets, previous);
    let pairs = 0;
    for (const entry of entries) {
      const seen = reach([entry], next);
      pairs += targets.filter((t) => seen.has(t)).length;
    }
    if (pairs > MAX_CONFIGURATIONS) error('每张拓扑最多生成 64 份可达配置');
    if (entries.length && targets.length && pairs === 0) issues.push({ level: 'warn', message: '还没有任何入口连通到目标，保存后不会生成配置' });
    // Without any connected pair the single hint above says it all.
    for (const v of pairs ? g.vertices : []) {
      if (issues.some((i) => i.level === 'error' && i.vertex === v.id && !i.edge)) continue;
      if (!fromEntry.has(v.id) || !toTarget.has(v.id))
        issues.push({ level: 'warn', message: '当前没有可达的入口—目标配置', vertex: v.id });
    }
  }
  return issues;
}
