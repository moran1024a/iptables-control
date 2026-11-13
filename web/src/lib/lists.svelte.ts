// List state that outlives a page visit: collapsed groups (saved) and filters (this tab only).

const KEY = 'ictl-collapsed';

function load(): string[] {
  try {
    const parsed = JSON.parse(localStorage.getItem(KEY) || '[]');
    return Array.isArray(parsed) ? parsed.filter((item) => typeof item === 'string') : [];
  } catch {
    return [];
  }
}

class Collapsed {
  private keys = $state<string[]>(load());

  has(kind: string, groupId: number) {
    return this.keys.includes(`${kind}-${groupId}`);
  }

  toggle(kind: string, groupId: number) {
    const key = `${kind}-${groupId}`;
    this.keys = this.keys.includes(key) ? this.keys.filter((k) => k !== key) : [...this.keys, key];
    this.save();
  }

  forget(kind: string, groupId: number) {
    this.keys = this.keys.filter((k) => k !== `${kind}-${groupId}`);
    this.save();
  }

  private save() {
    try {
      localStorage.setItem(KEY, JSON.stringify(this.keys));
    } catch {
      /* private mode */
    }
  }
}

export const collapsed = new Collapsed();

const stores = new Map<string, Record<string, string>>();

/** Reactive filter values for a list; kept while the panel stays open so returning from a detail page keeps them. */
export function filtersFor<T extends Record<string, string>>(kind: string, initial: T): T {
  if (!stores.has(kind)) {
    const state = $state({ ...initial });
    stores.set(kind, state);
  }
  return stores.get(kind) as T;
}

export const anyFilter = (filters: Record<string, string>) => Object.values(filters).some((value) => value.trim() !== '');
export function clearFilters(filters: Record<string, string>) {
  for (const key of Object.keys(filters)) filters[key] = '';
}
