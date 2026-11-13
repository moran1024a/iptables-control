import type { ExternalConfig, SubscriptionCandidate, TopologyConfig } from '$lib/types';

export type AnyConfig = TopologyConfig | ExternalConfig;

/** Display names of the protocols a configuration can speak. */
export const PROTOCOLS: Record<string, string> = {
  shadowsocks: 'Shadowsocks',
  'vless-enc': 'VLESS ENC',
  vless: 'VLESS',
  vmess: 'VMess',
  trojan: 'Trojan',
  hysteria2: 'Hysteria2',
  tuic: 'TUIC',
  anytls: 'AnyTLS',
  socks5: 'SOCKS5',
  http: 'HTTP/HTTPS',
  endpoint: 'IP／域名＋端口',
};
export const protocolName = (id: string) => PROTOCOLS[id] ?? id;

/** `host:port`, with IPv6 hosts in brackets. */
export const hostPort = (host: string, port: number | string) => `${host.includes(':') && !host.startsWith('[') ? `[${host}]` : host}:${port}`;

/** Topology and external ids overlap, so everything is keyed by source too. */
export const configKey = (config: { source: 'topology' | 'external'; id: number }) => `${config.source === 'topology' ? 'C' : 'E'}${config.id}`;

/** Number of relay nodes between entry and target on the applied (or, before that, the desired) path. */
export function relayCount(config: TopologyConfig): number {
  const applied = config.active_path.length > 0;
  const path = applied ? config.active_path : config.pending_path;
  const graph = applied ? config.active_graph : config.pending_graph;
  const kinds = new Map((graph?.vertices ?? []).map((vertex) => [vertex.id, vertex.kind]));
  // Without the graph snapshot every middle vertex counts; `direct` vertices only carry a weight.
  return path.slice(1, -1).filter((id) => (kinds.get(id) ?? 'node') === 'node').length;
}

/** The target endpoint, only when it is not the target's main address. */
export function targetExtra(config: TopologyConfig): string {
  const address = config.target_address;
  if (!address || address.id === 'main') return '';
  return address.name || hostPort(address.host, address.port);
}

/** Everything the search box matches against. */
export function configSearchText(config: AnyConfig): string {
  if (config.source === 'external')
    return [config.name, config.code, config.connection.host, config.connection.protocol, protocolName(config.connection.protocol), hostPort(config.connection.host, config.connection.port)].join('\n');
  const address = config.target_address;
  return [
    config.name,
    config.code,
    config.topology_name,
    `${config.entry_name} → ${config.target_name}`,
    config.connection ? hostPort(config.connection.host, config.connection.port) : '',
    config.connection ? `${config.connection.protocol} ${protocolName(config.connection.protocol)}` : '',
    ...config.entry_tags.map((tag) => tag.name),
    address ? `${address.name} ${hostPort(address.host, address.port)}` : '',
  ].join('\n');
}

/** Why a configuration is left out of subscriptions, per format; empty when it exports everywhere. */
export function compatibility(candidate: Pick<SubscriptionCandidate, 'error' | 'clash_error'> | undefined): { tone: 'none' | 'partial' | 'blocked'; text: string } {
  if (!candidate || (!candidate.error && !candidate.clash_error)) return { tone: 'none', text: '' };
  if (candidate.error && candidate.clash_error) {
    return {
      tone: 'blocked',
      text:
        candidate.error === candidate.clash_error
          ? `不能通过订阅下发：${candidate.error}`
          : `不能通过订阅下发：v2rayN ${candidate.error}；Mihomo ${candidate.clash_error}`,
    };
  }
  return {
    tone: 'partial',
    text: candidate.error ? `v2rayN 订阅会跳过：${candidate.error}` : `Mihomo 订阅会跳过：${candidate.clash_error}`,
  };
}
