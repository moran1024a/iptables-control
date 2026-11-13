// Poll results are new objects every few seconds. These helpers keep the previous reference while the relevant
// content is unchanged, so derived geometry (wire routing) is only recomputed when it has to be.

/** Returns the earlier value as long as `signature(value)` serialises to the same JSON. */
export function stableBy<T>(signature: (value: T) => unknown = (value) => value): (next: T) => T {
  let key: string | undefined;
  let kept: T;
  return (next) => {
    const k = JSON.stringify(signature(next));
    if (k !== key) [key, kept] = [k, next];
    return kept;
  };
}

interface WithAddresses {
  id: number;
  addresses: { id: string }[];
}
/** What card sizes and connector positions depend on: which addresses each resource has, in order. */
export const geometrySignature = (resources: { nodes: WithAddresses[]; targets: WithAddresses[] }) => [
  resources.nodes.map((n) => [n.id, n.addresses.map((a) => a.id)]),
  resources.targets.map((t) => [t.id, t.addresses.map((a) => a.id)]),
];
