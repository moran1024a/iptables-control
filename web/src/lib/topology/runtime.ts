// View-mode runtime data: which hops each configuration uses, derived from the link detail.
import type { ConfigView, FailoverPreview, Graph, NodeStatus, Topology, VertexId } from '$lib/types';
import { byCodePoint } from './colors';
import { edgeKey, nodeOf, targetOf, vertexName, vertexOf, type Catalog } from './model';
import type { Span } from './routing';

export interface RuntimeRoute {
  id: number | string;
  entry: VertexId;
  target: VertexId;
  path: VertexId[];
  /** The snapshot the path was compiled from; address selections are read from it. */
  graph: Graph | null;
  pending: boolean;
  state: string;
}

/** Applied and in-flight routes of every configuration (paths of at least one hop). */
export function routesOf(link: Pick<Topology, 'configurations'> | undefined): RuntimeRoute[] {
  return (link?.configurations ?? [])
    .flatMap((c: ConfigView): RuntimeRoute[] => [
      { id: c.id, entry: c.entry, target: c.target, path: c.active_path ?? [], graph: c.active_graph, pending: false, state: c.state },
      { id: c.id, entry: c.entry, target: c.target, path: c.pending_path ?? [], graph: c.pending_graph, pending: true, state: c.state },
    ])
    .filter((r) => r.path.length > 1);
}

/** Address selections of one hop, read from the route's own snapshot (falling back to the shown graph). */
export function hopAddresses(route: Pick<RuntimeRoute, 'graph'>, g: Graph, a: VertexId, b: VertexId): [string, string] {
  const binding = (route.graph ?? g).bindings?.[edgeKey(a, b)];
  return [binding?.from?.[a] || '', binding?.to?.[b] || ''];
}

const spanKey = (s: Pick<Span, 'entry' | 'a' | 'b' | 'addresses'>) => JSON.stringify([s.entry, s.a, s.b, s.addresses]);

/**
 * Hops to draw: one per (entry, hop, addresses, pending). A pending hop identical to a live one is dropped.
 * Order: entries in `order`, live before pending, then by vertex ids — this fixes the slot each colour gets.
 */
export function spansOf(routes: RuntimeRoute[], g: Graph, order: VertexId[]): Span[] {
  const hops = (r: RuntimeRoute): Span[] =>
    r.path.slice(1).map((b, i) => ({ a: r.path[i], b, entry: r.entry, pending: r.pending, addresses: hopAddresses(r, g, r.path[i], b) }));
  const live = new Set(routes.filter((r) => !r.pending).flatMap((r) => hops(r).map(spanKey)));
  const spans = new Map<string, Span>();
  for (const r of routes)
    for (const span of hops(r)) {
      if (r.pending && live.has(spanKey(span))) continue;
      spans.set(spanKey(span) + (span.pending ? '+' : ''), span);
    }
  const rank = (id: VertexId) => {
    const index = order.indexOf(id);
    return index < 0 ? order.length : index;
  };
  return [...spans.values()].sort(
    (p, q) => rank(p.entry) - rank(q.entry) || Number(p.pending) - Number(q.pending) || byCodePoint(p.a, q.a) || byCodePoint(p.b, q.b),
  );
}

const DOWN: NodeStatus[] = ['offline', 'error', 'skew', 'deleting', 'new'];

/** Hops ("entry|a|b") of applied routes whose every node is healthy: these get the flow animation. */
export function flowingHops(routes: RuntimeRoute[], g: Graph, cat: Catalog): Set<string> {
  const flowing = new Set<string>();
  for (const r of routes) {
    if (r.pending || r.state === 'deleting') continue;
    const healthy = r.path.every((id) => {
      const v = vertexOf(g, id);
      if (!v) return false;
      if (v.node_id == null) return true;
      const node = nodeOf(v, cat);
      return !!node && !DOWN.includes(node.status);
    });
    if (healthy) for (let i = 1; i < r.path.length; i++) flowing.add(`${r.entry}|${r.path[i - 1]}|${r.path[i]}`);
  }
  return flowing;
}

/** Entries whose applied route passes through each vertex. */
export function entriesThrough(routes: RuntimeRoute[]): Map<VertexId, VertexId[]> {
  const marks = new Map<VertexId, VertexId[]>();
  for (const r of routes) {
    if (r.pending) continue;
    for (const id of r.path) {
      const list = marks.get(id) ?? [];
      if (!list.includes(r.entry)) marks.set(id, [...list, r.entry]);
    }
  }
  return marks;
}

/** Addresses used by applied routes on each vertex: which rows receive (`in`) and send (`out`). */
export function usedPorts(routes: RuntimeRoute[], g: Graph): Map<VertexId, { in: Set<string>; out: Set<string> }> {
  const used = new Map<VertexId, { in: Set<string>; out: Set<string> }>();
  const at = (id: VertexId) => {
    if (!used.has(id)) used.set(id, { in: new Set(), out: new Set() });
    return used.get(id)!;
  };
  for (const r of routes) {
    if (r.pending) continue;
    const entry = (r.graph ?? g).vertices.find((v) => v.id === r.path[0]);
    if (entry?.address) at(entry.id).in.add(entry.address);
    for (let i = 1; i < r.path.length; i++) {
      const [out, into] = hopAddresses(r, g, r.path[i - 1], r.path[i]);
      if (out) at(r.path[i - 1]).out.add(out);
      if (into) at(r.path[i]).in.add(into);
    }
  }
  return used;
}

/** `entry → target[ · address]` label parts of a configuration row. */
export function configLabel(c: Pick<ConfigView, 'entry' | 'target' | 'target_address'>, g: Graph, cat: Catalog) {
  const target = vertexOf(g, c.target);
  const address = c.target_address && c.target_address.id !== 'main' ? c.target_address.name || `${c.target_address.host}:${c.target_address.port}` : '';
  return {
    entry: vertexName(vertexOf(g, c.entry), cat),
    target: targetOf(target, cat)?.name ?? vertexName(target, cat),
    address,
  };
}

/** Names of the hops between entry and target; `direct` for none, `等待生效` when nothing is applied. */
export function pathSummary(path: VertexId[], g: Graph, cat: Catalog): { text: string; full: string } {
  if (path.length < 2) return { text: '等待生效', full: '' };
  const names = path.map((id) => vertexName(vertexOf(g, id), cat));
  return { text: names.slice(1, -1).join(' → ') || 'direct', full: names.join(' → ') };
}

/** Hops of a failover candidate whose wiring differs from the applied route: only these are drawn. */
export function changedHops(row: FailoverPreview['paths'][number], preview: Graph, live: RuntimeRoute[], g: Graph): Span[] {
  const applied = new Set(
    live.filter((r) => !r.pending && r.entry === row.entry).flatMap((r) => r.path.slice(1).map((b, i) => JSON.stringify([r.path[i], b, hopAddresses(r, g, r.path[i], b)]))),
  );
  const out: Span[] = [];
  for (let i = 1; i < row.path.length; i++) {
    const a = row.path[i - 1];
    const b = row.path[i];
    const addresses = hopAddresses({ graph: preview }, g, a, b);
    if (!applied.has(JSON.stringify([a, b, addresses]))) out.push({ a, b, entry: row.entry, pending: true, addresses });
  }
  return out;
}
