import { describe, expect, it } from 'vitest';
import type { Subscription, SubscriptionCandidate } from '$lib/types';
import { incompatible, memberKeys, splitKeys, subscriptionUrl, unusable } from './model';

const item = (source: 'topology' | 'external', id: number, error = '', clash_error = ''): SubscriptionCandidate => ({
  id,
  source,
  name: `n${id}`,
  code: `${source === 'topology' ? 'C' : 'E'}000${id}`,
  group_id: 1,
  error,
  clash_error,
});
const catalog = [item('topology', 1, 'a', 'a'), item('topology', 2), item('external', 1), item('external', 2, 'x')];

describe('subscription model', () => {
  it('builds urls from the public url or the origin', () => {
    expect(subscriptionUrl('', 'http://10.0.0.1:8080', '/sub/abc', 'clash')).toBe('http://10.0.0.1:8080/sub/abc?format=clash');
    expect(subscriptionUrl('https://p.example.com/', 'http://x', '/sub/abc', 'v2ray')).toBe('https://p.example.com/sub/abc?format=v2ray');
  });

  it('keys members by source', () => {
    expect(memberKeys({ configuration_ids: [1, 2], external_configuration_ids: [1] })).toEqual(['C1', 'C2', 'E1']);
  });

  it('splits keys in catalog order and drops vanished ones', () => {
    expect(splitKeys(['E2', 'C9', 'E1', 'C2'], catalog)).toEqual({ configuration_ids: [2], external_configuration_ids: [1, 2] });
  });

  it('finds incompatible members', () => {
    const subscription = { configuration_ids: [1, 2], external_configuration_ids: [2, 7] } as Subscription;
    const map = new Map(catalog.map((entry) => [`${entry.source === 'topology' ? 'C' : 'E'}${entry.id}`, entry]));
    expect(incompatible(subscription, map).map((entry) => entry.code)).toEqual(['C0001', 'E0002']);
    expect(unusable(catalog[0])).toBe(true);
    expect(unusable(catalog[3])).toBe(false);
  });
});
