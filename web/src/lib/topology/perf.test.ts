import { expect, it } from 'vitest';
import { AUTO, type Graph, type PaletteNode, type PaletteTarget, type Vertex } from '$lib/types';
import { boxesOf, catalogOf, emptyGraph } from './model';
import { pathBlocked, routeConnections, routeSpans, type Span } from './routing';

/** 2 entries, 4 relay columns, 3 targets: 40 vertices and about 80 edges, laid out like the server does. */
export function bigGraph(): { g: Graph; nodes: PaletteNode[]; targets: PaletteTarget[] } {
  const nodes: PaletteNode[] = Array.from({ length: 12 }, (_, i) => ({
    id: i + 1, name: `n${i + 1}`, addr: `10.0.0.${i + 1}`, weight: 0, group_id: 1, tags: [], mode: 'active', status: 'synced',
    addresses: [{ id: 'main', primary: true, name: '', host: `10.0.0.${i + 1}`, local: '', device: '', gateway: '' }],
    network: { addresses: [], sampled_at: null, complete: false },
  }));
  const targets: PaletteTarget[] = [1, 2, 3].map((id) => ({
    id, name: `t${id}`, type: 'endpoint', host: `t${id}`, port: 443, params: '{}', group_id: 1, sort: id, addresses: [{ id: 'main', name: '默认', host: `t${id}`, port: 443 }],
  }));
  const g = emptyGraph();
  const sizes = [2, 9, 9, 9, 8, 3];
  const columns = sizes.map((size, l) =>
    Array.from({ length: size }, (_, i) => {
      const v: Vertex = { id: `L${l}n${i}`, kind: l === 0 ? 'entry' : l === 5 ? 'target' : 'node', x: 40 + l * 400, y: 60 + i * 158 + (9 - size) * 79, disabled: false };
      if (l === 5) v.target_id = i + 1;
      else v.node_id = ((l * 3 + i) % 12) + 1;
      g.vertices.push(v);
      return v.id;
    }),
  );
  for (let l = 0; l < 5; l++)
    columns[l].forEach((a, i) => {
      for (const k of l === 0 ? [0, 1, 2, 3, 4] : [0, 1]) {
        const b = columns[l + 1][(i * (l === 0 ? 4 : 1) + k) % columns[l + 1].length];
        if (g.edges.some((e) => e[0] === a && e[1] === b)) continue;
        g.edges.push([a, b]);
        g.bindings[`${a}/${b}`] = { from: { [a]: AUTO }, to: { [b]: 'main' } };
      }
    });
  return { g, nodes, targets };
}

it('routes a 40-vertex, 80-edge graph quickly and without crossing cards', () => {
  const { g, nodes, targets } = bigGraph();
  expect(g.vertices).toHaveLength(40);
  expect(g.edges.length).toBeGreaterThanOrEqual(76);
  const cat = catalogOf({ nodes, targets });
  const boxes = boxesOf(g, cat);
  const started = performance.now();
  const routes = routeConnections(g, boxes, cat, { editing: true });
  const elapsed = performance.now() - started;
  expect(routes).toHaveLength(g.edges.length);
  for (const r of routes) {
    const others = [...boxes.values()].filter((b) => b.id !== r.a && b.id !== r.b);
    expect(pathBlocked(r.points, others, 4), r.key).toBe(false);
  }
  // Six configurations, each a 5-hop route.
  const spans: Span[] = [];
  for (const entry of ['L0n0', 'L0n1'])
    for (let k = 0; k < 3; k++) {
      let at = entry;
      for (let l = 0; l < 5; l++) {
        const next = g.edges.filter((e) => e[0] === at)[k % 2]?.[1] ?? g.edges.find((e) => e[0] === at)![1];
        spans.push({ a: at, b: next, entry, pending: false, addresses: [AUTO, 'main'] });
        at = next;
      }
    }
  const t1 = performance.now();
  const overlay = routeSpans(g, spans, boxes, cat);
  const spent = performance.now() - t1;
  expect(elapsed).toBeLessThan(250);
  expect(spent).toBeLessThan(100);
});
