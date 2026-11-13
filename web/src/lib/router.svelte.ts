// Hash routing: the server only ever serves one document under the secret panel path.
import { confirm, type ConfirmRequest } from './confirm.svelte';

function parse(hash: string) {
  const [path = '', query = ''] = hash.replace(/^#\/?/, '').split('?');
  return { parts: path.split('/').filter(Boolean), query: new URLSearchParams(query) };
}

/** A blocker returns a prompt while leaving the page would lose work, otherwise null. */
export type Blocker = () => ConfirmRequest | null;

class Router {
  parts = $state<string[]>([]);
  query = $state(new URLSearchParams());
  private blockers = new Set<Blocker>();
  private current = typeof location === 'undefined' ? '' : location.hash;
  private restoring = false;

  constructor() {
    this.sync();
    if (typeof window === 'undefined') return;
    window.addEventListener('hashchange', () => void this.changed());
    window.addEventListener('beforeunload', (event) => {
      if (this.pending()) event.preventDefault();
    });
  }

  private pending(): ConfirmRequest | null {
    for (const blocker of this.blockers) {
      const prompt = blocker();
      if (prompt) return prompt;
    }
    return null;
  }

  private async changed() {
    if (this.restoring) {
      this.restoring = false;
      return;
    }
    const target = location.hash;
    const prompt = target === this.current ? null : this.pending();
    if (prompt) {
      // Put the address back first so the page underneath stays consistent while asking.
      this.restoring = true;
      history.replaceState(null, '', this.current || '#/');
      this.restoring = false;
      if (!(await confirm(prompt))) return;
      this.blockers.clear();
      location.hash = target;
      return;
    }
    this.current = target;
    this.sync();
  }

  /** Register from `$effect`; the returned function unregisters. */
  block(blocker: Blocker) {
    this.blockers.add(blocker);
    return () => {
      this.blockers.delete(blocker);
    };
  }

  private sync() {
    const next = parse(typeof location === 'undefined' ? '' : location.hash);
    this.parts = next.parts;
    this.query = next.query;
  }

  /** First segment, e.g. `nodes`. */
  get page() {
    return this.parts[0] ?? '';
  }

  /** Numeric id in the given position, or null. */
  id(index = 1): number | null {
    const raw = this.parts[index];
    return raw && /^\d+$/.test(raw) ? Number(raw) : null;
  }

  go(path: string, options: { replace?: boolean } = {}) {
    const hash = '#/' + path.replace(/^#?\/?/, '');
    if (options.replace) {
      history.replaceState(null, '', hash);
      this.current = hash;
      this.sync();
    } else location.hash = hash;
  }
}

export const router = new Router();
export const href = (path: string) => '#/' + path.replace(/^\/+/, '');
