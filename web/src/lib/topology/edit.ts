// Pure graph mutations of the editor. Every function returns a new graph and leaves its input untouched,
// so undo history can keep plain references.
import { AUTO, type EdgeKey, type Graph, type Vertex, type VertexId } from '$lib/types';
import { addressesOf, boxesOf, cardHeight, cardWidth, connections, edgeKey, vertexName, vertexOf, type Box, type Catalog, type Edge } from './model';
import { linkError } from './rules';

export type Result = { graph: Graph; error?: undefined } | { error: string; graph?: undefined };
type Binding = Graph['bindings'][EdgeKey];

export const newVertexId = (): VertexId =>
  typeof crypto !== 'undefined' && 'randomUUID' in crypto
    ? crypto.randomUUID()
    : 'v' + Math.random().toString(36).slice(2) + Date.now().toString(36);

const withVertex = (g: Graph, id: VertexId, change: (v: Vertex) => Vertex): Graph => ({
  ...g,
  vertices: g.vertices.map((v) => (v.id === id ? change(v) : v)),
});

function omit<T extends Record<string, unknown>>(record: T, drop: (key: string) => boolean): T {
  return Object.fromEntries(Object.entries(record ?? {}).filter(([key]) => !drop(key))) as T;
}

/** Shift a new card to the right of whatever it would cover (20 px margin). */
export function freePosition(boxes: Iterable<Box>, x: number, y: number, width: number, height: number): { x: number; y: number } {
  const list = [...boxes];
  for (let tries = 0; tries <= list.length; tries++) {
    const hit = list.find((b) => x < b.x + b.width + 20 && x + width > b.x - 20 && y < b.y + b.height + 20 && y + height > b.y - 20);
    if (!hit) break;
    x = hit.x + hit.width + 40;
  }
  return { x: Math.round(x), y: Math.round(y) };
}

export interface NewVertex {
  id: VertexId;
  kind: Vertex['kind'];
  /** node id for entry/node, target id for target. */
  resource?: number;
  x: number;
  y: number;
  /** Treat x/y as the wanted centre and avoid covering other cards. */
  centre?: boolean;
}

export function addVertex(g: Graph, cat: Catalog, spec: NewVertex): Graph {
  let v: Vertex = { id: spec.id, kind: spec.kind, x: Math.round(spec.x), y: Math.round(spec.y), disabled: false };
  if (spec.kind === 'direct') v.weight = 0;
  else if (spec.kind === 'target') v.target_id = spec.resource;
  else {
    v.node_id = spec.resource;
    v.out_address = AUTO;
  }
  const addresses = addressesOf(g, v, cat);
  if (spec.kind !== 'direct' && addresses.length === 1) {
    v.in_address = addresses[0].id;
    if (spec.kind === 'entry') v.address = addresses[0].id;
  }
  if (spec.centre) {
    const width = cardWidth(v);
    const height = cardHeight(v, addresses.length);
    v = { ...v, ...freePosition(boxesOf(g, cat).values(), spec.x - width / 2, spec.y - height / 2, width, height) };
  }
  return { ...g, vertices: [...g.vertices, v] };
}

export function moveVertex(g: Graph, id: VertexId, x: number, y: number): Graph {
  return withVertex(g, id, (v) => ({ ...v, x: Math.round(x), y: Math.round(y) }));
}

/**
 * The receive address to use when a wire is dropped on a card rather than on one IP:
 * the requested one when valid, a target's default, the only one, or null when the user has to choose.
 */
export function receiveAddress(g: Graph, to: VertexId, cat: Catalog, wanted?: string | null): string | null {
  const v = vertexOf(g, to);
  if (!v || v.kind === 'direct') return null;
  const addresses = addressesOf(g, v, cat);
  if (wanted && addresses.some((a) => a.id === wanted)) return wanted;
  // A target's default endpoint is always `main` (the server assumes the same); other endpoints are picked on the IP row or the wire.
  if (v.kind === 'target' && addresses.some((a) => a.id === 'main')) return 'main';
  return addresses.length === 1 ? addresses[0].id : null;
}

/** Connect `from -> to` with the given egress selection and receive address. */
export function connect(g: Graph, cat: Catalog, from: VertexId, to: VertexId, out: string | null, into: string | null): Result {
  const error = linkError(g, from, to, cat);
  if (error) return { error };
  const a = vertexOf(g, from)!;
  const b = vertexOf(g, to)!;
  const binding: Binding = { from: {}, to: {} };
  if (a.kind !== 'direct') binding.from[from] = out ?? AUTO;
  if (b.kind !== 'direct') {
    if (!into) return { error: `请选择「${vertexName(b, cat)}」的接收 IP` };
    binding.to[to] = into;
  }
  return {
    graph: {
      ...g,
      vertices: g.vertices.map((v) =>
        v.id === from && a.kind !== 'direct'
          ? { ...v, out_address: out ?? AUTO }
          : v.id === to && b.kind !== 'direct' && into
            ? { ...v, in_address: into }
            : v,
      ),
      edges: [...g.edges, [from, to]],
      bindings: { ...g.bindings, [edgeKey(from, to)]: binding },
    },
  };
}

/** Change the address of one end of one edge; other edges of the vertex keep theirs. */
export function setEdgeAddress(g: Graph, edge: Edge, side: 'in' | 'out', address: string | null): Graph {
  const key = edgeKey(edge[0], edge[1]);
  const old = g.bindings[key] ?? { from: {}, to: {} };
  const binding: Binding =
    side === 'out' ? { from: { [edge[0]]: address }, to: old.to ?? {} } : { from: old.from ?? {}, to: { [edge[1]]: address } };
  const owner = side === 'out' ? edge[0] : edge[1];
  const next = { ...g, bindings: { ...g.bindings, [key]: binding } };
  if (!address) return next;
  return withVertex(next, owner, (v) => (side === 'out' ? { ...v, out_address: address } : { ...v, in_address: address }));
}

/** The listen address of an entry. */
export function setEntryAddress(g: Graph, id: VertexId, address: string | null): Graph {
  return withVertex(g, id, (v) => ({ ...v, address, ...(address ? { in_address: address } : {}) }));
}

/** Entry listen port for one (entry, target) pair; null returns to automatic allocation. */
export function setPort(g: Graph, entry: VertexId, target: VertexId, port: number | null): Graph {
  const key = edgeKey(entry, target);
  const ports = omit(g.ports ?? {}, (k) => k === key);
  if (port !== null) ports[key] = port;
  return { ...g, ports };
}

export function setWeight(g: Graph, id: VertexId, weight: number): Graph {
  return withVertex(g, id, (v) => ({ ...v, weight }));
}

/** Point a card at another node/target. Address choices of the old resource are dropped and re-defaulted. */
export function replaceResource(g: Graph, cat: Catalog, id: VertexId, resource: number): Graph {
  const v = vertexOf(g, id);
  if (!v || v.kind === 'direct') return g;
  const { address: _a, in_address: _i, out_address: _o, ...rest } = v;
  const fresh: Vertex = v.kind === 'target' ? { ...rest, target_id: resource } : { ...rest, node_id: resource };
  const base = { ...g, vertices: g.vertices.map((x) => (x.id === id ? fresh : x)) };
  const addresses = addressesOf(base, fresh, cat);
  const only = addresses.length === 1 ? addresses[0].id : null;
  const into = v.kind === 'target' ? 'main' : only;
  const next: Vertex = { ...fresh };
  if (v.kind !== 'target') next.out_address = AUTO;
  if (into) next.in_address = into;
  if (v.kind === 'entry' && only) next.address = only;
  const bindings = { ...g.bindings };
  for (const [a, b] of connections(g)) {
    if (a !== id && b !== id) continue;
    const key = edgeKey(a, b);
    const old = bindings[key] ?? { from: {}, to: {} };
    bindings[key] = {
      from: a === id ? { [a]: AUTO } : (old.from ?? {}),
      to: b === id ? (into ? { [b]: into } : {}) : (old.to ?? {}),
    };
  }
  return { ...base, vertices: base.vertices.map((x) => (x.id === id ? next : x)), bindings };
}

export function removeEdge(g: Graph, edge: Edge): Graph {
  const key = edgeKey(edge[0], edge[1]);
  return {
    ...g,
    edges: g.edges.filter(([a, b]) => a !== edge[0] || b !== edge[1]),
    bindings: omit(g.bindings, (k) => k === key),
  };
}

/** Put a new direct (weight 0) on an edge: A -> B becomes A -> D -> B, keeping both address selections. */
export function insertDirect(g: Graph, cat: Catalog, edge: Edge, id: VertexId): Result {
  const [a, b] = edge;
  const va = vertexOf(g, a);
  const vb = vertexOf(g, b);
  const index = g.edges.findIndex((e) => e[0] === a && e[1] === b);
  if (!va || !vb || index < 0) return { error: '连接的节点已不存在' };
  if (va.kind === 'direct' || vb.kind === 'direct') return { error: 'direct 不能连续连接' };
  const boxes = boxesOf(g, cat);
  const ba = boxes.get(a)!;
  const bb = boxes.get(b)!;
  const direct: Vertex = {
    id,
    kind: 'direct',
    weight: 0,
    disabled: false,
    x: Math.round((ba.x + ba.width + bb.x - 110) / 2),
    y: Math.round((ba.y + ba.height / 2 + bb.y + bb.height / 2) / 2 - 28),
  };
  const old = g.bindings[edgeKey(a, b)] ?? { from: {}, to: {} };
  const edges = [...g.edges];
  edges.splice(index, 1, [a, id], [id, b]);
  return {
    graph: {
      ...g,
      vertices: [...g.vertices, direct],
      edges,
      bindings: {
        ...omit(g.bindings, (k) => k === edgeKey(a, b)),
        [edgeKey(a, id)]: { from: old.from ?? {}, to: {} },
        [edgeKey(id, b)]: { from: {}, to: old.to ?? {} },
      },
    },
  };
}

/** Remove a card with its wires. A fully wired direct is replaced by the plain wire it sat on. */
export function removeVertex(g: Graph, id: VertexId): Result {
  const v = vertexOf(g, id);
  if (!v) return { graph: g };
  const edges = connections(g);
  const incoming = edges.find((e) => e[1] === id);
  const outgoing = edges.find((e) => e[0] === id);
  const rejoin = v.kind === 'direct' && incoming && outgoing ? ([incoming[0], outgoing[1]] as Edge) : null;
  if (rejoin && edges.some((e) => e[0] === rejoin[0] && e[1] === rejoin[1])) return { error: '两端已有另一条直连，请先删除其中一条连线' };
  const touches = (key: string) => key.split('/').includes(id);
  const next: Graph = {
    ...g,
    vertices: g.vertices.filter((x) => x.id !== id),
    edges: g.edges.filter(([a, b]) => a !== id && b !== id),
    bindings: omit(g.bindings, touches),
    ports: omit(g.ports ?? {}, touches),
  };
  if (!rejoin) return { graph: next };
  const index = g.edges.findIndex((e) => e[1] === id);
  const joined = [...g.edges];
  joined.splice(index, 1, rejoin);
  return {
    graph: {
      ...next,
      edges: joined.filter(([a, b]) => a !== id && b !== id),
      bindings: {
        ...next.bindings,
        [edgeKey(rejoin[0], rejoin[1])]: {
          from: g.bindings[edgeKey(incoming![0], id)]?.from ?? {},
          to: g.bindings[edgeKey(id, outgoing![1])]?.to ?? {},
        },
      },
    },
  };
}

/** The graph as sent to the server: only the fields it reads. */
export function outgoing(g: Graph): Graph {
  return { ...g, version: 2, routing_version: 2, address_book: {} };
}
