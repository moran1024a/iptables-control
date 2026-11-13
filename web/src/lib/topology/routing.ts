// Wire geometry: orthogonal polylines with rounded corners, routed around cards and away from each other.
import { AUTO, type Graph, type VertexId } from '$lib/types';
import { addressesOf, connections, portAddress, portPoint, type Box, type Catalog, type Edge, type Point, type Side } from './model';

export type Segment = [Point, Point];

const round = (n: number) => Math.round(n * 10) / 10;
const xy = (p: Point) => `${round(p[0])} ${round(p[1])}`;

/** Drop repeated points and merge collinear runs. */
export function simplify(points: Point[]): Point[] {
  const p: Point[] = [];
  for (const v of points) {
    const last = p[p.length - 1];
    if (last && last[0] === v[0] && last[1] === v[1]) continue;
    while (p.length > 1) {
      const a = p[p.length - 2];
      const b = p[p.length - 1];
      if ((a[0] === b[0] && b[0] === v[0]) || (a[1] === b[1] && b[1] === v[1])) p.pop();
      else break;
    }
    p.push(v);
  }
  return p;
}

/** SVG path through the points with corners rounded by at most 8 px. */
export function roundedPath(points: Point[]): string {
  const p = simplify(points);
  if (!p.length) return '';
  let d = `M ${xy(p[0])}`;
  for (let i = 1; i < p.length - 1; i++) {
    const a = p[i - 1];
    const b = p[i];
    const c = p[i + 1];
    const before = Math.hypot(b[0] - a[0], b[1] - a[1]);
    const after = Math.hypot(c[0] - b[0], c[1] - b[1]);
    const r = Math.min(8, before / 2, after / 2);
    const from: Point = [b[0] + ((a[0] - b[0]) * r) / before, b[1] + ((a[1] - b[1]) * r) / before];
    const to: Point = [b[0] + ((c[0] - b[0]) * r) / after, b[1] + ((c[1] - b[1]) * r) / after];
    d += ` L ${xy(from)} Q ${xy(b)} ${xy(to)}`;
  }
  return p.length > 1 ? d + ` L ${xy(p[p.length - 1])}` : d;
}

/** True when a segment of the polyline passes through a box grown by `padding`. Touching the grown edge is allowed. */
export function pathBlocked(points: Point[], boxes: Box[], padding = 12): boolean {
  for (let i = 1; i < points.length; i++) {
    const p = points[i - 1];
    const q = points[i];
    boxes: for (const b of boxes) {
      const lo = [b.x - padding, b.y - padding];
      const hi = [b.x + b.width + padding, b.y + b.height + padding];
      let start = 0;
      let end = 1;
      for (let axis = 0; axis < 2; axis++) {
        const delta = q[axis] - p[axis];
        if (!delta) {
          if (p[axis] <= lo[axis] || p[axis] >= hi[axis]) continue boxes;
          continue;
        }
        const a = (lo[axis] - p[axis]) / delta;
        const c = (hi[axis] - p[axis]) / delta;
        start = Math.max(start, Math.min(a, c));
        end = Math.min(end, Math.max(a, c));
        if (start >= end) continue boxes;
      }
      return true;
    }
  }
  return false;
}

/** Cost of laying a segment next to or across already placed ones. */
export function lanePenalty(a: Point, b: Point, used: Segment[], clearance = 6): number {
  const horizontal = a[1] === b[1];
  const axis = horizontal ? 0 : 1;
  const other = 1 - axis;
  let cost = 0;
  for (const [c, d] of used) {
    const same = horizontal === (c[1] === d[1]);
    if (same) {
      const overlap = Math.min(Math.max(a[axis], b[axis]), Math.max(c[axis], d[axis])) - Math.max(Math.min(a[axis], b[axis]), Math.min(c[axis], d[axis]));
      if (overlap > 2 && Math.abs(a[other] - c[other]) < clearance) cost += 1000 + overlap * 15;
    } else if (
      c[axis] > Math.min(a[axis], b[axis]) &&
      c[axis] < Math.max(a[axis], b[axis]) &&
      a[other] > Math.min(c[other], d[other]) &&
      a[other] < Math.max(c[other], d[other])
    )
      cost += 35;
  }
  return cost;
}

interface SearchNode {
  x: number;
  y: number;
  dir: number;
  cost: number;
  rank: number;
  prev: SearchNode | null;
}

/** Min-heap on `rank`. */
class Heap {
  private items: SearchNode[] = [];
  get size() {
    return this.items.length;
  }
  push(node: SearchNode) {
    const items = this.items;
    let i = items.push(node) - 1;
    while (i > 0) {
      const parent = (i - 1) >> 1;
      if (items[parent].rank <= items[i].rank) break;
      [items[parent], items[i]] = [items[i], items[parent]];
      i = parent;
    }
  }
  pop(): SearchNode {
    const items = this.items;
    const top = items[0];
    const last = items.pop()!;
    if (items.length) {
      items[0] = last;
      for (let i = 0; ; ) {
        const left = 2 * i + 1;
        const right = left + 1;
        let best = i;
        if (left < items.length && items[left].rank < items[best].rank) best = left;
        if (right < items.length && items[right].rank < items[best].rank) best = right;
        if (best === i) break;
        [items[best], items[i]] = [items[i], items[best]];
        i = best;
      }
    }
    return top;
  }
}

interface Area {
  x0: number;
  x1: number;
  y0: number;
  y1: number;
}

/**
 * A* over a sparse orthogonal grid from `exit` to `enter`. Grid lines run beside every card and beside wires
 * already placed; a turn costs more than a short detour. With an `area`, only that part of the canvas is searched.
 */
function search(exit: Point, enter: Point, allBoxes: Box[], allUsed: Segment[], gap: number, clearance: number, area: Area | null): Point[] | null {
  const boxes = area ? allBoxes.filter((b) => b.x - gap <= area.x1 && b.x + b.width + gap >= area.x0 && b.y - gap <= area.y1 && b.y + b.height + gap >= area.y0) : allBoxes;
  const used = area
    ? allUsed.filter(([a, b]) => Math.min(a[0], b[0]) <= area.x1 && Math.max(a[0], b[0]) >= area.x0 && Math.min(a[1], b[1]) <= area.y1 && Math.max(a[1], b[1]) >= area.y0)
    : allUsed;
  const lines = (k: 0 | 1, own: number[]) => {
    const lo = area ? (k === 0 ? area.x0 : area.y0) : -Infinity;
    const hi = area ? (k === 0 ? area.x1 : area.y1) : Infinity;
    const values = [
      ...boxes.flatMap((b) => (k === 0 ? [b.x - gap, b.x + b.width + gap] : [b.y - gap, b.y + b.height + gap])),
      ...used.flatMap(([a, b]) => (a[k] === b[k] ? [a[k] - 8, a[k] + 8] : [])),
    ].filter((v) => v >= lo && v <= hi);
    return [...new Set([...own, ...values])].sort((a, b) => a - b);
  };
  const xs = lines(0, [exit[0], enter[0]]);
  const ys = lines(1, [exit[1], enter[1]]);
  const sx = xs.indexOf(exit[0]);
  const sy = ys.indexOf(exit[1]);
  const tx = xs.indexOf(enter[0]);
  const ty = ys.indexOf(enter[1]);
  const key = (x: number, y: number, dir: number) => (x * ys.length + y) * 2 + dir;
  // Length plus lane penalty of one grid edge (Infinity through a card); each edge is looked at from several states.
  const edges = new Map<number, number>();
  const edgeCost = (x: number, y: number, dir: number): number => {
    const k = key(x, y, dir);
    let cost = edges.get(k);
    if (cost === undefined) {
      const a: Point = [xs[x], ys[y]];
      const b: Point = dir === 0 ? [xs[x + 1], ys[y]] : [xs[x], ys[y + 1]];
      cost = pathBlocked([a, b], boxes) ? Infinity : Math.abs(a[0] - b[0]) + Math.abs(a[1] - b[1]) + lanePenalty(a, b, used, clearance);
      edges.set(k, cost);
    }
    return cost;
  };
  const open = new Heap();
  open.push({ x: sx, y: sy, dir: 0, cost: 0, rank: 0, prev: null });
  const costs = new Map<number, number>([[key(sx, sy, 0), 0]]);
  for (let visits = 0; open.size && visits < 12000; visits++) {
    const n = open.pop();
    if (n.x === tx && n.y === ty) {
      const path: Point[] = [];
      for (let at: SearchNode | null = n; at; at = at.prev) path.push([xs[at.x], ys[at.y]]);
      return path.reverse();
    }
    if (n.cost > (costs.get(key(n.x, n.y, n.dir)) ?? Infinity)) continue;
    for (const [dx, dy, dir] of STEPS) {
      const x = n.x + dx;
      const y = n.y + dy;
      if (x < 0 || y < 0 || x >= xs.length || y >= ys.length) continue;
      const length = edgeCost(Math.min(x, n.x), Math.min(y, n.y), dir);
      if (length === Infinity) continue;
      const cost = n.cost + length + (dir !== n.dir ? 38 : 0);
      const k = key(x, y, dir);
      if (cost >= (costs.get(k) ?? Infinity)) continue;
      costs.set(k, cost);
      open.push({ x, y, dir, cost, prev: n, rank: cost + Math.abs(xs[x] - enter[0]) + Math.abs(ys[y] - enter[1]) });
    }
  }
  return null;
}
const STEPS = [
  [-1, 0, 0],
  [1, 0, 0],
  [0, -1, 1],
  [0, 1, 1],
] as const;

export interface RouteOptions {
  /** Lane index: spreads the stubs and bends of wires sharing a port. */
  index?: number;
  used?: Segment[];
  clearance?: number;
  /** Horizontal shift of the bend of a simple three-segment wire. */
  bendOffset?: number | null;
  /** Skip the grid search (used while a card is being dragged). */
  quick?: boolean;
}

/**
 * Route one wire from an out connector to an in connector.
 * Tries a straight line, then a three-segment elbow, then an A* search over a sparse orthogonal grid,
 * and finally goes over the top of everything.
 */
export function routePoints(start: Point, end: Point, source: Box, target: Box, boxes: Box[], options: RouteOptions = {}): Point[] {
  const { index = 0, used = [], clearance = 6, bendOffset = null, quick = false } = options;
  const penalty = (a: Point, b: Point) => lanePenalty(a, b, used, clearance);
  const room = end[0] - start[0];
  const lane = 24 + (index % 5) * 8;
  const gap = room > 0 ? Math.min(lane, room / 3) : lane;
  const exit: Point = [start[0] + gap, start[1]];
  const enter: Point = [end[0] - gap, end[1]];
  const obstacles = boxes.filter((b) => b.id !== source.id && b.id !== target.id);
  if (end[0] > start[0] && start[1] === end[1] && !pathBlocked([start, end], obstacles) && penalty(start, end) === 0) return [start, end];
  if (enter[0] >= exit[0]) {
    const x = Math.max(exit[0], Math.min(enter[0], (exit[0] + enter[0]) / 2 + (bendOffset ?? ((index % 5) - 2) * 8)));
    const p: Point[] = [start, [x, start[1]], [x, end[1]], end];
    if (quick) return p;
    if (!pathBlocked(p, obstacles) && p.slice(1).every((b, i) => penalty(p[i], b) === 0)) return p;
  }
  if (!quick) {
    // Most wires join neighbouring columns: search the area around both ends first, the whole canvas only if that fails.
    const near = { x0: Math.min(exit[0], enter[0]) - 80, x1: Math.max(exit[0], enter[0]) + 80, y0: Math.min(exit[1], enter[1]) - 220, y1: Math.max(exit[1], enter[1]) + 220 };
    const found = search(exit, enter, boxes, used, gap, clearance, near) ?? search(exit, enter, boxes, used, gap, clearance, null);
    if (found) return [start, ...found, end];
  }
  const top = Math.min(...boxes.map((b) => b.y)) - gap;
  return [start, exit, [exit[0], top], [enter[0], top], enter, end];
}

function segmentsOf(points: Point[]): Segment[] {
  const out: Segment[] = [];
  for (let i = 1; i < points.length; i++) out.push([points[i - 1], points[i]]);
  return out;
}

/** Midpoint along a polyline, for labels and buttons on a wire. */
export function midpoint(points: Point[]): Point {
  const segments = segmentsOf(points);
  const total = segments.reduce((sum, [a, b]) => sum + Math.hypot(b[0] - a[0], b[1] - a[1]), 0);
  let walked = 0;
  for (const [a, b] of segments) {
    const length = Math.hypot(b[0] - a[0], b[1] - a[1]);
    if (walked + length >= total / 2 && length > 0) {
      const t = (total / 2 - walked) / length;
      return [a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t];
    }
    walked += length;
  }
  return points[0] ?? [0, 0];
}

interface End {
  point: Point;
  spacing: number;
  lane: number;
  offset: number;
}

export interface WireRoute {
  key: string;
  a: VertexId;
  b: VertexId;
  index: number;
  points: Point[];
  d: string;
  arrowHeight: number;
}

/**
 * Route every design edge. Ends sharing one connector are fanned out vertically in the order of
 * the opposite card (y, then x, then edge index); wires are placed left to right so later ones avoid earlier lanes.
 */
export function routeConnections(
  g: Graph,
  boxes: Map<VertexId, Box>,
  cat: Catalog,
  options: { editing?: boolean; moving?: VertexId | null; previous?: WireRoute[] } = {},
): WireRoute[] {
  const { editing = false, moving = null, previous = [] } = options;
  const byId = new Map(g.vertices.map((v) => [v.id, v]));
  type Draft = { a: VertexId; b: VertexId; i: number; out?: End; in?: End };
  const routes: Draft[] = connections(g)
    .filter(([a, b]) => boxes.has(a) && boxes.has(b))
    .map(([a, b], i) => ({ a, b, i }));
  const groups = new Map<string, { r: Draft; side: Side; point: Point }[]>();
  for (const r of routes)
    for (const [side, id] of [
      ['out', r.a],
      ['in', r.b],
    ] as [Side, VertexId][]) {
      const v = byId.get(id)!;
      const address = portAddress(g, v, side, [r.a, r.b], cat);
      const point = portPoint(boxes.get(id)!, v, addressesOf(g, v, cat), side, address);
      const key = JSON.stringify([id, side, address]);
      if (!groups.has(key)) groups.set(key, []);
      groups.get(key)!.push({ r, side, point });
    }
  for (const group of groups.values()) {
    group.sort((p, q) => {
      const x = boxes.get(p.side === 'out' ? p.r.b : p.r.a)!;
      const y = boxes.get(q.side === 'out' ? q.r.b : q.r.a)!;
      return x.y - y.y || x.x - y.x || p.r.i - q.r.i;
    });
    const spacing = Math.min(9, 18 / Math.max(1, group.length - 1));
    group.forEach((p, i) => {
      const offset = (i - (group.length - 1) / 2) * spacing;
      if (editing) p.point[0] += p.side === 'out' ? 9 : -9;
      p.point[1] += offset;
      p.r[p.side] = { point: p.point, spacing, lane: i, offset };
    });
  }
  const order = [...routes].sort((p, q) => {
    const x = boxes.get(p.a)!;
    const y = boxes.get(q.a)!;
    return x.x - y.x || x.y - y.y || p.i - q.i;
  });
  const all = [...boxes.values()];
  const used: Segment[] = [];
  const done = new Map<Draft, WireRoute>();
  // While a card is dragged, wires that do not touch it stay where they were.
  if (moving) {
    const prior = new Map(previous.map((r) => [r.key, r]));
    for (const r of order) {
      const old = prior.get(`${r.a}/${r.b}`);
      if (r.a === moving || r.b === moving || !old) continue;
      done.set(r, { ...old, index: r.i });
      used.push(...segmentsOf(old.points));
    }
  }
  for (const r of order) {
    if (done.has(r)) continue;
    const spacing = Math.min(r.in!.spacing, r.out!.spacing);
    const points = routePoints(r.out!.point, r.in!.point, boxes.get(r.a)!, boxes.get(r.b)!, all, {
      index: r.i,
      used,
      clearance: Math.min(6, spacing * 0.8),
      quick: !!moving,
    });
    done.set(r, { key: `${r.a}/${r.b}`, a: r.a, b: r.b, index: r.i, points, d: roundedPath(points), arrowHeight: Math.min(5, r.in!.spacing * 0.65) });
    used.push(...segmentsOf(points));
  }
  return routes.map((r) => done.get(r)!);
}

/** One hop of one entry's route as drawn in view mode. */
export interface Span {
  a: VertexId;
  b: VertexId;
  entry: VertexId;
  pending: boolean;
  /** [egress selection at a, receive address at b]; '' when unknown. */
  addresses: [string, string];
}

export interface SpanRoute extends Span {
  key: string;
  points: Point[];
  d: string;
  stroke: number;
  halo: number;
  arrowHeight: number;
}

/**
 * Route the per-entry runtime spans. On an out connector all spans of one entry (and one pending state)
 * leave from the same slot; on an in connector every span gets its own slot so arrow heads never stack.
 */
export function routeSpans(g: Graph, spans: Span[], boxes: Map<VertexId, Box>, cat: Catalog): SpanRoute[] {
  const byId = new Map(g.vertices.map((v) => [v.id, v]));
  type Draft = Span & { out?: End; in?: End };
  const list: Draft[] = spans.filter((s) => boxes.has(s.a) && boxes.has(s.b)).map((s) => ({ ...s }));
  const ports = new Map<string, { span: Draft; side: Side; point: Point }[]>();
  for (const span of list)
    for (const [side, id] of [
      ['out', span.a],
      ['in', span.b],
    ] as [Side, VertexId][]) {
      const v = byId.get(id)!;
      const address = span.addresses[side === 'out' ? 0 : 1] || (side === 'out' ? AUTO : '');
      const key = JSON.stringify([id, side, address]);
      if (!ports.has(key)) ports.set(key, []);
      ports.get(key)!.push({ span, side, point: portPoint(boxes.get(id)!, v, addressesOf(g, v, cat), side, address) });
    }
  for (const ends of ports.values()) {
    const slot = (c: { span: Draft; side: Side }): unknown => (c.side === 'out' ? JSON.stringify([c.span.entry, c.span.pending]) : c.span);
    const slots = [...new Set(ends.map(slot))];
    const spacing = Math.min(8, 16 / Math.max(1, slots.length - 1));
    for (const c of ends) {
      const lane = slots.indexOf(slot(c));
      const offset = (lane - (slots.length - 1) / 2) * spacing;
      c.point[1] += offset;
      c.span[c.side] = { point: c.point, spacing, lane, offset };
    }
  }
  const used: { entry: VertexId; pending: boolean; segment: Segment }[] = [];
  const all = [...boxes.values()];
  return list.map((span) => {
    const start = span.out!.point;
    const end = span.in!.point;
    // Parallel colours need opposite bend offsets when going down or up, or they cross.
    const bend = (-Math.sign(end[1] - start[1]) * (span.out!.offset + span.in!.offset)) / 2;
    const points = routePoints(start, end, boxes.get(span.a)!, boxes.get(span.b)!, all, {
      index: span.in!.lane,
      used: used.filter((s) => s.entry !== span.entry || s.pending !== span.pending).map((s) => s.segment),
      clearance: 6,
      bendOffset: bend,
    });
    for (const segment of segmentsOf(points))
      if (Math.hypot(segment[1][0] - segment[0][0], segment[1][1] - segment[0][1]) > 4) used.push({ entry: span.entry, pending: span.pending, segment });
    const spacing = Math.min(span.out!.spacing, span.in!.spacing);
    return {
      a: span.a,
      b: span.b,
      entry: span.entry,
      pending: span.pending,
      addresses: span.addresses,
      key: JSON.stringify([span.entry, span.a, span.b, span.addresses, span.pending]),
      points,
      d: roundedPath(points),
      stroke: Math.min(2, Math.max(1.2, spacing * 0.45)),
      halo: Math.min(6, spacing * 0.75),
      arrowHeight: Math.min(6, span.in!.spacing * 0.65),
    };
  });
}

/** Rubber band from a connector to the pointer while a wire is being drawn. */
export function elbow(start: Point, end: Point): string {
  const x = end[0] > start[0] + 48 ? (start[0] + end[0]) / 2 : start[0] + 24;
  return roundedPath([start, [x, start[1]], [x, end[1]], end]);
}

export type { Edge };
