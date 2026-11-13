import { describe, expect, it } from 'vitest';
import type { ExternalConfig, TopologyConfig } from '$lib/types';
import { compatibility, configKey, configSearchText, hostPort, relayCount, targetExtra } from './model';

const topology = (over: Partial<TopologyConfig> = {}) =>
  ({
    source: 'topology',
    id: 1,
    name: '配置 0001',
    code: 'C0001',
    topology_name: '主线路',
    entry_name: '香港入口',
    entry_tags: [{ id: 1, name: 'HK', color: '#ef4444' }],
    target_name: '业务服务',
    active_path: ['v1', 'v2', 'd1', 't1'],
    pending_path: [],
    active_graph: { vertices: [{ id: 'v1', kind: 'entry' }, { id: 'v2', kind: 'node' }, { id: 'd1', kind: 'direct' }, { id: 't1', kind: 'target' }] },
    pending_graph: null,
    connection: { protocol: 'endpoint', host: '2001:db8::1', port: 443, uri: '[2001:db8::1]:443' },
    target_address: { id: 'bk', name: '', host: '198.51.100.2', port: 8443 },
    ...over,
  }) as unknown as TopologyConfig;

describe('config model', () => {
  it('formats host and port', () => {
    expect(hostPort('example.com', 443)).toBe('example.com:443');
    expect(hostPort('2001:db8::1', 443)).toBe('[2001:db8::1]:443');
    expect(hostPort('[::1]', 80)).toBe('[::1]:80');
  });

  it('keys by source', () => {
    expect(configKey({ source: 'topology', id: 1 })).toBe('C1');
    expect(configKey({ source: 'external', id: 1 })).toBe('E1');
  });

  it('counts relay nodes, ignoring weight-only vertices', () => {
    expect(relayCount(topology())).toBe(1);
    expect(relayCount(topology({ active_path: ['v1', 't1'] }))).toBe(0);
    expect(relayCount(topology({ active_path: [], pending_path: ['v1', 'a', 'b', 't1'], pending_graph: null }))).toBe(2);
  });

  it('names a non-main target address', () => {
    expect(targetExtra(topology())).toBe('198.51.100.2:8443');
    expect(targetExtra(topology({ target_address: { id: 'main', name: '默认', host: 'h', port: 1 } }))).toBe('');
  });

  it('builds search text', () => {
    const text = configSearchText(topology());
    for (const part of ['C0001', '主线路', '香港入口 → 业务服务', '[2001:db8::1]:443', 'HK', '198.51.100.2:8443']) expect(text).toContain(part);
    const external = { source: 'external', id: 1, name: 'x', code: 'E0001', connection: { protocol: 'trojan', host: 'example.com', port: 443, uri: '' } } as ExternalConfig;
    expect(configSearchText(external)).toContain('Trojan');
    expect(configSearchText(external)).toContain('example.com:443');
  });

  it('explains subscription compatibility', () => {
    expect(compatibility(undefined).tone).toBe('none');
    expect(compatibility({ error: '', clash_error: '' }).tone).toBe('none');
    expect(compatibility({ error: 'a', clash_error: 'a' })).toEqual({ tone: 'blocked', text: '不能通过订阅下发：a' });
    expect(compatibility({ error: 'a', clash_error: 'b' }).text).toBe('不能通过订阅下发：v2rayN a；Mihomo b');
    expect(compatibility({ error: 'a', clash_error: '' })).toEqual({ tone: 'partial', text: 'v2rayN 订阅会跳过：a' });
    expect(compatibility({ error: '', clash_error: 'b' }).text).toBe('Mihomo 订阅会跳过：b');
  });
});
