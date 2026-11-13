// Snapshot undo/redo. States are immutable values kept by reference; equality is decided by the caller's key.

export const HISTORY_LIMIT = 50;

export interface History<T> {
  past: T[];
  present: T;
  future: T[];
}

export const createHistory = <T>(present: T): History<T> => ({ past: [], present, future: [] });

/** Record a new state. A state equal to the present one (same key) is not recorded. */
export function record<T>(h: History<T>, next: T, key: (value: T) => string = JSON.stringify): History<T> {
  if (next === h.present || key(next) === key(h.present)) return h;
  return { past: [...h.past, h.present].slice(-HISTORY_LIMIT), present: next, future: [] };
}

/** Replace the present state without a history step (typing, dragging in progress). */
export const amend = <T>(h: History<T>, present: T): History<T> => ({ ...h, present });

export function undo<T>(h: History<T>): History<T> {
  if (!h.past.length) return h;
  return { past: h.past.slice(0, -1), present: h.past[h.past.length - 1], future: [h.present, ...h.future] };
}

export function redo<T>(h: History<T>): History<T> {
  if (!h.future.length) return h;
  return { past: [...h.past, h.present], present: h.future[0], future: h.future.slice(1) };
}
