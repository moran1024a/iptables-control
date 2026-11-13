import type { Subscription, SubscriptionCandidate } from '$lib/types';
import { configKey } from '../configs/model';

export const FORMATS = [
  { id: 'clash', label: 'Mihomo', hint: 'Mihomo / Clash 系客户端，YAML 配置' },
  { id: 'v2ray', label: 'v2rayN', hint: 'v2rayN 等支持 Base64 分享链接列表的客户端' },
] as const;
export type Format = (typeof FORMATS)[number]['id'];

/** Subscription links live at the server root, outside the panel's secret path. */
export function subscriptionUrl(publicUrl: string, origin: string, path: string, format: Format): string {
  return `${(publicUrl.trim() || origin).replace(/\/+$/, '')}${path}?format=${format}`;
}

/** Keys (`C<id>` / `E<id>`) of everything a subscription includes. */
export const memberKeys = (subscription: Pick<Subscription, 'configuration_ids' | 'external_configuration_ids'>) => [
  ...subscription.configuration_ids.map((id) => `C${id}`),
  ...subscription.external_configuration_ids.map((id) => `E${id}`),
];

/** Split selected keys into the two id lists the API takes, in catalog order and without entries that no longer exist. */
export function splitKeys(selected: Iterable<string>, catalog: SubscriptionCandidate[]) {
  const chosen = new Set(selected);
  const members = catalog.filter((item) => chosen.has(configKey(item)));
  return {
    configuration_ids: members.filter((item) => item.source === 'topology').map((item) => item.id),
    external_configuration_ids: members.filter((item) => item.source === 'external').map((item) => item.id),
  };
}

/** Members that at least one format leaves out. */
export function incompatible(subscription: Subscription, catalog: Map<string, SubscriptionCandidate>): SubscriptionCandidate[] {
  return memberKeys(subscription)
    .map((key) => catalog.get(key))
    .filter((item): item is SubscriptionCandidate => !!item && !!(item.error || item.clash_error));
}

/** A configuration no format can export may not be newly added. */
export const unusable = (item: SubscriptionCandidate) => !!item.error && !!item.clash_error;
