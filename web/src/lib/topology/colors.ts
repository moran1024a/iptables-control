// Per-entry route colours. Deterministic for a given set of entry ids, readable on light and dark surfaces,
// and always paired with a short ordinal label so colour is never the only cue.
import type { Graph, VertexId } from '$lib/types';

/** Categorical palette (colour-blind friendly order); mid lightness so it holds on both themes. */
export const PALETTE = ['#2f7fd6', '#e07a12', '#12a37b', '#c2509c', '#8a63d2', '#c8a000', '#d9483b', '#1aa5c4'];

export interface EntryStyle {
  color: string;
  /** 1-based ordinal shown next to the colour. */
  ordinal: number;
}

/** Stable 32-bit string hash (same fold as the previous panel). */
export function hashId(id: string): number {
  let hash = 0;
  for (let i = 0; i < id.length; i++) hash = (hash * 31 + id.charCodeAt(i)) >>> 0;
  return hash;
}

/** Code-point string order; unlike `localeCompare` it does not depend on the browser locale. */
export const byCodePoint = (a: string, b: string) => (a < b ? -1 : a > b ? 1 : 0);

/**
 * Assign a style to every entry vertex (runtime-only ones included).
 * Entries are taken in id order. Each takes the palette slot its id hashes to, or the next free one;
 * past the palette size, hues are spread by the golden angle away from the hues already used.
 */
export function entryStyles(g: Pick<Graph, 'vertices'>): Map<VertexId, EntryStyle> {
  const styles = new Map<VertexId, EntryStyle>();
  const taken = new Set<number>();
  const hues: number[] = [];
  const ids = g.vertices
    .filter((v) => v.kind === 'entry')
    .map((v) => v.id)
    .sort(byCodePoint);
  for (const id of ids) {
    const hash = hashId(id);
    let color: string;
    if (taken.size < PALETTE.length) {
      let slot = hash % PALETTE.length;
      while (taken.has(slot)) slot = (slot + 1) % PALETTE.length;
      taken.add(slot);
      color = PALETTE[slot];
    } else {
      let hue = hash % 360;
      const near = (h: number) => Math.min(Math.abs(h - hue), 360 - Math.abs(h - hue)) < 28;
      for (let i = 0; i < 12 && hues.some(near); i++) hue = (hue + 137.5) % 360;
      hues.push(hue);
      color = `hsl(${hue.toFixed(1)} 62% 50%)`;
    }
    styles.set(id, { color, ordinal: styles.size + 1 });
  }
  return styles;
}
