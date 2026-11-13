// Shared model of the topology canvas: resources, card geometry and address lookups.
// Card sizes mirror the server layout (server/topology.py arrange), so server-computed positions never overlap.
import { AUTO, type EdgeKey, type Graph, type PaletteNode, type PaletteTarget, type Vertex, type VertexId } from '$lib/types';

export const CARD_WIDTH = 220;
export const DIRECT_WIDTH = 110;
export const DIRECT_HEIGHT = 56;
export const HEAD_HEIGHT = 62;
export const ROW_HEIGHT = 24;

export type Side = 'in' | 'out';
export type Edge = [VertexId, VertexId];
export type Point = [number, number];
export interface Box {
  id: VertexId;
  x: number;
  y: number;
  width: number;
  height: number;
}
/** One IP row of a card. `port` only exists on target addresses. */
export interface AddressRow {
  id: string;
  name: string;
  host: string;
  port?: number;
  primary?: boolean;
}

export interface Catalog {
  nodes: Map<number, PaletteNode>;
  targets: Map<number, PaletteTarget>;
}

export function catalogOf(resources: { nodes: PaletteNode[]; targets: PaletteTarget[] } | undefined): Catalog {
  return {
    nodes: new Map((resources?.nodes ?? []).map((n) => [n.id, n])),
    targets: new Map((resources?.targets ?? []).map((t) => [t.id, t])),
  };
}

export const emptyGraph = (): Graph => ({
  version: 2,
  routing_version: 2,
  vertices: [],
  edges: [],
  bindings: {},
  ports: {},
  address_book: {},
});

export const edgeKey = (a: VertexId, b: VertexId) => `${a}/${b}` as EdgeKey;
export const vertexOf = (g: Graph, id: VertexId | null | undefined) => (id ? g.vertices.find((v) => v.id === id) : undefined);
export const nodeOf = (v: Vertex | undefined, cat: Catalog) => (v?.node_id != null ? cat.nodes.get(v.node_id) : undefined);
export const targetOf = (v: Vertex | undefined, cat: Catalog) => (v?.target_id != null ? cat.targets.get(v.target_id) : undefined);

/** Edges without duplicates, in first-seen order. */
export function connections(g: Graph): Edge[] {
  const seen = new Set<string>();
  const out: Edge[] = [];
  for (const [a, b] of g.edges) {
    const key = edgeKey(a, b);
    if (!seen.has(key)) {
      seen.add(key);
      out.push([a, b]);
    }
  }
  return out;
}

/** Display name of a vertex: the resource name, `direct`, or a placeholder when the resource is gone. */
export function vertexName(v: Vertex | undefined, cat: Catalog): string {
  if (!v) return '未知节点';
  if (v.kind === 'direct') return 'direct';
  return (v.kind === 'target' ? targetOf(v, cat)?.name : nodeOf(v, cat)?.name) ?? '已删除';
}

/** Addresses of a vertex: the live resource first, the saved snapshot when the resource is gone. */
export function addressesOf(g: Graph, v: Vertex | undefined, cat: Catalog): AddressRow[] {
  if (!v || v.kind === 'direct') return [];
  const live = v.kind === 'target' ? targetOf(v, cat)?.addresses : nodeOf(v, cat)?.addresses;
  return live ?? Object.values(g.address_book?.[v.id] ?? {});
}

/** `host[:port]` of an address row. */
export const addressText = (a: AddressRow) => (a.port ? `${a.host}:${a.port}` : a.host);

export const cardWidth = (v: Vertex) => (v.kind === 'direct' ? DIRECT_WIDTH : CARD_WIDTH);
/** Rows on a card: one per address (at least one) plus the automatic egress row of entries and relays. */
export const cardRows = (v: Vertex, addressCount: number) => Math.max(1, addressCount) + (v.node_id != null ? 1 : 0);
export const cardHeight = (v: Vertex, addressCount: number) =>
  v.kind === 'direct' ? DIRECT_HEIGHT : HEAD_HEIGHT + ROW_HEIGHT * cardRows(v, addressCount);

export function boxesOf(g: Graph, cat: Catalog): Map<VertexId, Box> {
  return new Map(
    g.vertices.map((v) => [
      v.id,
      { id: v.id, x: v.x, y: v.y, width: cardWidth(v), height: cardHeight(v, addressesOf(g, v, cat).length) },
    ]),
  );
}

/** World rectangle around all cards, with room for the column captions. */
export function worldOf(boxes: Iterable<Box>): { x: number; y: number; width: number; height: number } {
  const list = [...boxes];
  if (!list.length) return { x: 0, y: 0, width: 700, height: 320 };
  const x = Math.min(...list.map((b) => b.x - 60));
  const y = Math.min(...list.map((b) => b.y - 100));
  return {
    x,
    y,
    width: Math.max(700, ...list.map((b) => b.x + b.width + 60 - x)),
    height: Math.max(320, ...list.map((b) => b.y + b.height + 60 - y)),
  };
}

/** Address selection stored on an edge end; `null` when the edge has none. */
export function boundAddress(g: Graph, edge: Edge, side: Side): string | null {
  const binding = g.bindings?.[edgeKey(edge[0], edge[1])];
  return (side === 'out' ? binding?.from?.[edge[0]] : binding?.to?.[edge[1]]) || null;
}

/**
 * Which address a port of the vertex shows as "in use".
 * With an edge: that edge's binding. Without: the value all edges on this side agree on,
 * else the vertex preference, else the natural default.
 */
export function portAddress(g: Graph, v: Vertex, side: Side, edge: Edge | null, cat: Catalog): string {
  if (v.kind === 'entry' && side === 'in') return v.address || '';
  if (edge) return boundAddress(g, edge, side) || (v.kind === 'target' ? 'main' : '');
  const values = connections(g)
    .filter((e) => e[side === 'in' ? 1 : 0] === v.id)
    .map((e) => boundAddress(g, e, side))
    .filter((a): a is string => !!a);
  if (values.length && new Set(values).size === 1) return values[0];
  const preferred = side === 'in' ? v.in_address : v.out_address;
  if (preferred) return preferred;
  if (v.kind === 'target') return 'main';
  if (side === 'out') return AUTO;
  const addresses = addressesOf(g, v, cat);
  return addresses.length === 1 ? addresses[0].id : '';
}

/** Row index of an address on the card; the automatic egress row sits below all addresses. */
export function addressRow(addresses: AddressRow[], address: string | null): number {
  if (address === AUTO) return Math.max(1, addresses.length);
  const index = addresses.findIndex((a) => a.id === address);
  return index >= 0 ? index : 0;
}

/** Anchor of a connector in world coordinates. */
export function portPoint(box: Box, v: Vertex, addresses: AddressRow[], side: Side, address: string | null): Point {
  const x = side === 'out' ? box.x + box.width : box.x;
  if (v.kind === 'direct') return [x, box.y + box.height / 2];
  return [x, box.y + HEAD_HEIGHT + addressRow(addresses, address) * ROW_HEIGHT + ROW_HEIGHT / 2];
}

/** Column captions: one per layer of the server layout. */
export function layerLabels(g: Graph): { key: string; text: string; x: number; y: number }[] {
  const columns = new Map<number, Vertex[]>();
  for (const v of g.vertices) {
    if (v.kind === 'direct' || v.runtime_only || !Number.isInteger(v.layer)) continue;
    const layer = v.layer as number;
    columns.set(layer, [...(columns.get(layer) ?? []), v]);
  }
  return [...columns.entries()]
    .sort((a, b) => a[0] - b[0])
    .map(([layer, list]) => ({
      key: String(layer),
      text: layer === 0 ? '入口' : list.every((v) => v.kind === 'target') ? '目标' : `第 ${layer} 层`,
      x: list[0].x,
      y: Math.min(...list.map((v) => v.y)) - 35,
    }));
}
