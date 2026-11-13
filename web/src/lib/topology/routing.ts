// Wire geometry: orthogonal polylines with rounded corners, routed through the channels between card columns.
import { AUTO, type Graph, type VertexId } from '$lib/types';
import { addressesOf, connections, portAddress, portPoint, type Box, type Catalog, type Edge, type Point, type Side } from './model';

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

// ---- channel routing --------------------------------------------------------------------------------------------
// Cards form columns (groups overlapping horizontally) with free channels between them. A wire runs left to
// right: straight through every column it passes, changing height only inside channels. All vertical runs of
// one channel are placed together, ordered so that they cross as little as possible.

export interface WireEnds {
  start: Point;
  end: Point;
  a: VertexId;
  b: VertexId;
}

const CARD_GAP = 14;
const BEND = 30;

interface Column {
  x0: number;
  x1: number;
  /** Free vertical intervals between the cards, top to bottom. */
  free: [number, number][];
}

function columnsOf(boxes: Box[]): {
  columns: Column[];
  index: Map<VertexId, number>;
} {
  const columns: (Column & { boxes: Box[] })[] = [];
  const index = new Map<VertexId, number>();
  for (const b of [...boxes].sort((p, q) => p.x - q.x || p.y - q.y)) {
    let c = columns[columns.length - 1];
    if (!c || b.x >= c.x1 + 24) columns.push((c = { x0: b.x, x1: b.x + b.width, free: [], boxes: [] }));
    c.x1 = Math.max(c.x1, b.x + b.width);
    c.boxes.push(b);
    index.set(b.id, columns.length - 1);
  }
  for (const c of columns) {
    let top = -Infinity;
    for (const b of c.boxes.sort((p, q) => p.y - q.y)) {
      if (b.y - CARD_GAP >= top) c.free.push([top, b.y - CARD_GAP]);
      top = Math.max(top, b.y + b.height + CARD_GAP);
    }
    c.free.push([top, Infinity]);
  }
  return { columns, index };
}

/** Heights at which a wire passes the columns between its ends: least vertical travel, then fewest bends. */
function passHeights(from: number, to: number, columns: Column[]): number[] {
  if (!columns.length) return [];
  const values = [...new Set([from, to, ...columns.flatMap((c) => c.free.flat())])].filter(Number.isFinite).sort((p, q) => p - q);
  const step = (p: number, q: number) => (p === q ? 0 : Math.abs(p - q) + BEND);
  let prev = [{ y: from, cost: 0, path: [] as number[] }];
  // Equal routes change height as late as possible, so wires leaving one connector stay together.
  let late = columns.length;
  for (const c of columns) {
    late--;
    const next: typeof prev = [];
    for (const y of values) {
      if (!c.free.some(([lo, hi]) => y >= lo && y <= hi)) continue;
      let best = prev[0];
      let cost = Infinity;
      for (const p of prev) {
        const total = p.cost + step(p.y, y) + (p.y === y ? 0 : late * 0.01 + 0.01);
        if (total < cost) [best, cost] = [p, total];
      }
      next.push({ y, cost, path: [...best.path, y] });
    }
    prev = next;
  }
  return prev.reduce((p, q) => (q.cost + step(q.y, to) < p.cost + step(p.y, to) ? q : p)).path;
}

interface Net {
  key: string;
  /** Height on the left side of the channel and heights on the right side. */
  s: number;
  ts: number[];
  lo: number;
  hi: number;
  lane: number;
}

/** Cost of net `n` turning left of net `m`: crossings, and a heavy charge for two wires on one line. */
function sideCost(n: Net, m: Net): number {
  let cost = n.lo + 1 < m.s && m.s < n.hi - 1 ? 1 : 0;
  for (const t of n.ts) {
    if (Math.abs(t - m.s) < 5) cost += 1000;
    else if (m.lo + 1 < t && t < m.hi - 1) cost++;
  }
  return cost;
}

/** Order the nets of one channel left to right and give nets that never meet the same lane. */
function assignLanes(nets: Net[]): number {
  const order: Net[] = [];
  for (const n of [...nets].sort((p, q) => p.lo - q.lo || p.hi - q.hi || (p.key < q.key ? -1 : 1))) {
    let cost = order.reduce((sum, m) => sum + sideCost(n, m), 0);
    let best = cost;
    let at = 0;
    order.forEach((m, i) => {
      cost += sideCost(m, n) - sideCost(n, m);
      if (cost < best) [best, at] = [cost, i + 1];
    });
    order.splice(at, 0, n);
  }
  for (let pass = 0; pass < 3; pass++) {
    let swapped = false;
    for (let i = 1; i < order.length; i++)
      if (sideCost(order[i], order[i - 1]) < sideCost(order[i - 1], order[i])) {
        [order[i - 1], order[i]] = [order[i], order[i - 1]];
        swapped = true;
      }
    if (!swapped) break;
  }
  let lanes = 0;
  order.forEach((n, i) => {
    n.lane = 0;
    for (let j = 0; j < i; j++) if (order[j].lo < n.hi + 8 && n.lo < order[j].hi + 8) n.lane = Math.max(n.lane, order[j].lane + 1);
    lanes = Math.max(lanes, n.lane + 1);
  });
  return lanes;
}

/** A wire whose target is not in a later column: a plain elbow when it fits, else the shortest loop around. */
function detour(w: WireEnds, index: number, boxes: Box[]): Point[] {
  const { start, end } = w;
  const others = boxes.filter((b) => b.id !== w.a && b.id !== w.b);
  const shift = (index % 5) * 6;
  if (end[0] - start[0] >= 36) {
    const x = Math.max(start[0] + 12, Math.min(end[0] - 12, (start[0] + end[0]) / 2 + shift - 12));
    const p: Point[] = [start, [x, start[1]], [x, end[1]], end];
    if (!pathBlocked(p, others, 6)) return p;
  }
  const exit = start[0] + 20 + shift;
  const enter = end[0] - 20 - shift;
  let best: Point[] = [];
  let least = Infinity;
  for (const y of boxes.flatMap((b) => [b.y - 16 - shift, b.y + b.height + 16 + shift])) {
    const middle: Point[] = [
      [exit, start[1]],
      [exit, y],
      [enter, y],
      [enter, end[1]],
    ];
    const cost = Math.abs(y - start[1]) + Math.abs(y - end[1]) + (pathBlocked(middle, boxes, 4) ? 5000 : 0);
    if (cost < least) [best, least] = [middle, cost];
  }
  return [start, ...best, end];
}

/** Route wires between cards. `start` lies on the right edge of card `a`, `end` on the left edge of card `b`. */
export function routeWires(wires: WireEnds[], boxes: Box[]): Point[][] {
  const { columns, index } = columnsOf(boxes);
  // Per wire: its height in every column from the source to the target column, and the shared trunk so far.
  const plans = wires.map((w) => {
    const from = index.get(w.a)!;
    const to = index.get(w.b)!;
    if (!(from < to)) return null;
    const ys = [w.start[1], ...passHeights(w.start[1], w.end[1], columns.slice(from + 1, to)), w.end[1]];
    const trunks = [`${w.start[0]},${w.start[1]}`];
    for (let i = 1; i < ys.length - 1; i++) trunks.push(`${trunks[i - 1]}>${ys[i]}`);
    return { from, ys, trunks, bias: w.start[1] + w.end[1] };
  });
  // Wires passing a column at the same height are spread out; wires of one trunk stay together.
  columns.forEach((column, c) => {
    const tracks = new Map<string, { y: number; bias: number; set: (y: number) => void }>();
    for (const p of plans) {
      const i = p ? c - p.from : 0;
      if (!p || i < 1 || i > p.ys.length - 2) continue;
      const key = p.trunks[i];
      const track = tracks.get(key) ?? {
        y: p.ys[i],
        bias: p.bias,
        set: () => {},
      };
      const earlier = track.set;
      track.set = (y) => (earlier(y), (p.ys[i] = y));
      tracks.set(key, track);
    }
    for (const [lo, hi] of column.free) {
      const inside = [...tracks.values()].filter((t) => t.y >= lo && t.y <= hi).sort((p, q) => p.y - q.y || p.bias - q.bias);
      const gap = Math.min(10, (hi - lo) / Math.max(1, inside.length - 1));
      let last = -Infinity;
      const ys = inside.map((t) => (last = Math.max(t.y, last + gap)));
      inside.forEach((t, i) => t.set(Math.min(ys[i], hi - (inside.length - 1 - i) * gap)));
    }
  });
  // Spreading leaves small steps between neighbouring columns; take them out where the next height is free here too.
  const taken = columns.map(() => [] as number[]);
  for (const p of plans) p?.ys.slice(1, -1).forEach((y, i) => taken[p.from + i + 1].push(y));
  for (let round = 0; round < 4; round++)
    for (const p of plans)
      for (let i = (p?.ys.length ?? 0) - 3; p && i >= 1; i--) {
        const c = p.from + i;
        const [y, next] = [p.ys[i], p.ys[i + 1]];
        if (y === next || Math.abs(y - next) > 24 || !columns[c].free.some(([lo, hi]) => next >= lo && next <= hi && y >= lo && y <= hi)) continue;
        if (taken[c].some((other) => other !== y && Math.abs(other - next) < 6) || taken[c].filter((other) => other === y).length > 1) continue;
        taken[c][taken[c].indexOf(y)] = p.ys[i] = next;
      }
  // Lanes: one vertical run per trunk and channel.
  const lanes = new Map<string, number>();
  for (let c = 0; c + 1 < columns.length; c++) {
    const nets = new Map<string, Net>();
    for (const p of plans) {
      const i = p ? c - p.from : -1;
      if (!p || i < 0 || i > p.ys.length - 2) continue;
      const net = nets.get(p.trunks[i]) ?? {
        key: p.trunks[i],
        s: p.ys[i],
        ts: [],
        lo: p.ys[i],
        hi: p.ys[i],
        lane: 0,
      };
      net.ts.push(p.ys[i + 1]);
      net.lo = Math.min(net.lo, p.ys[i + 1]);
      net.hi = Math.max(net.hi, p.ys[i + 1]);
      nets.set(net.key, net);
    }
    const bent = [...nets.values()].filter((n) => n.hi - n.lo > 0.5);
    const count = assignLanes(bent);
    const x0 = columns[c].x1;
    const width = columns[c + 1].x0 - x0;
    const margin = Math.min(22, width / 4);
    const gap = Math.min(12, (width - 2 * margin) / Math.max(1, count - 1));
    for (const n of bent) lanes.set(`${c}|${n.key}`, x0 + width / 2 + (n.lane - (count - 1) / 2) * gap);
  }
  return wires.map((w, n) => {
    const p = plans[n];
    if (!p) return simplify(detour(w, n, boxes));
    const points: Point[] = [w.start];
    for (let i = 0; i + 1 < p.ys.length; i++) {
      const x = lanes.get(`${p.from + i}|${p.trunks[i]}`);
      if (x !== undefined && Math.abs(p.ys[i] - p.ys[i + 1]) > 0.5) points.push([x, p.ys[i]], [x, p.ys[i + 1]]);
    }
    points.push(w.end);
    return simplify(points);
  });
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
export function routeConnections(g: Graph, boxes: Map<VertexId, Box>, cat: Catalog, options: { editing?: boolean } = {}): WireRoute[] {
  const { editing = false } = options;
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
  const lines = routeWires(
    routes.map((r) => ({
      start: r.out!.point,
      end: r.in!.point,
      a: r.a,
      b: r.b,
    })),
    [...boxes.values()],
  );
  return routes.map((r, n) => ({
    key: `${r.a}/${r.b}`,
    a: r.a,
    b: r.b,
    index: r.i,
    points: lines[n],
    d: roundedPath(lines[n]),
    arrowHeight: Math.min(5, r.in!.spacing * 0.65),
  }));
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
      ports.get(key)!.push({
        span,
        side,
        point: portPoint(boxes.get(id)!, v, addressesOf(g, v, cat), side, address),
      });
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
  const lines = routeWires(
    list.map((span) => ({
      start: span.out!.point,
      end: span.in!.point,
      a: span.a,
      b: span.b,
    })),
    [...boxes.values()],
  );
  return list.map((span, n) => {
    const spacing = Math.min(span.out!.spacing, span.in!.spacing);
    return {
      a: span.a,
      b: span.b,
      entry: span.entry,
      pending: span.pending,
      addresses: span.addresses,
      key: JSON.stringify([span.entry, span.a, span.b, span.addresses, span.pending]),
      points: lines[n],
      d: roundedPath(lines[n]),
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
