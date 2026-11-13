import { describe, expect, it } from 'vitest';
import type { Target } from '$lib/types';
import { buildSave, checkAddresses, copyValue, draftFor, parsePort, typeLabel, type TargetDraft } from './target';

const endpoint: Target = {
  id: 1, name: 'web', type: 'endpoint', host: '203.0.113.10', port: 443, params: {}, group_id: 2, sort: 1, configs: [], uri: '203.0.113.10:443',
  addresses: [
    { id: 'main', name: '默认', host: '203.0.113.10', port: 443 },
    { id: 'a-1', name: '备用', host: '203.0.113.11', port: 443 },
  ],
};
const ss: Target = {
  ...endpoint, id: 2, name: 'ss', type: 'shadowsocks', uri: 'ss://abc@203.0.113.9:8388#ss', host: '203.0.113.9', port: 8388,
  params: { method: 'aes-256-gcm', password: 'p', share_query: [] }, addresses: [{ id: 'main', name: '默认', host: '203.0.113.9', port: 8388 }],
};
const draft = (patch: Partial<TargetDraft>): TargetDraft => ({ name: '', group: '', host: '', port: '', uri: '', addresses: [], ...patch });
const field = (run: () => unknown) => {
  try {
    run();
  } catch (err) {
    return (err as { field?: string }).field;
  }
  return null;
};

describe('targets', () => {
  it('labels protocols', () => {
    expect(typeLabel(endpoint)).toBe('地址');
    expect(typeLabel(ss)).toBe('SS');
    expect(typeLabel({ type: 'shadowsocks', params: { method: '2022-blake3-aes-128-gcm', password: '', share_query: [] } })).toBe('SS2022');
    expect(typeLabel({ type: 'vless-enc', params: { uuid: '', share_query: [] } })).toBe('VLESS');
    expect(copyValue(endpoint)).toBe('203.0.113.10:443');
    expect(copyValue(ss)).toBe(ss.uri);
  });

  it('parses ports', () => {
    expect(parsePort(' 443 ')).toBe(443);
    for (const bad of ['0', '65536', '1.5', '-1', '', 'abc', '080']) expect(parsePort(bad)).toBeNull();
  });

  it('drops the synthesized main address from the draft', () => {
    expect(draftFor(endpoint, '2').addresses.map((row) => row.id)).toEqual(['a-1']);
  });

  it('builds a manual create body without empty name or group', () => {
    expect(buildSave(draft({ host: ' example.com ', port: '80' }), 'manual', null)).toEqual({ host: 'example.com', port: 80, addresses: [] });
  });

  it('sends the link only when it changed on edit', () => {
    const same = draftFor(ss, '2');
    expect(buildSave({ ...same, name: 'renamed' }, 'link', ss)).toEqual({ name: 'renamed', group_id: 2, addresses: [] });
    expect(buildSave({ ...same, uri: 'ss://new' }, 'link', ss)).toMatchObject({ uri: 'ss://new' });
  });

  it('routes validation errors to fields', () => {
    expect(field(() => buildSave(draft({ port: '80' }), 'manual', null))).toBe('host');
    expect(field(() => buildSave(draft({ host: 'a.com', port: '0' }), 'manual', null))).toBe('port');
    expect(field(() => buildSave(draft({}), 'link', null))).toBe('uri');
    expect(field(() => buildSave({ ...draftFor(endpoint, '2'), name: ' ' }, 'manual', endpoint))).toBe('name');
    const rows = [{ key: 'k', id: 'k', name: '', host: 'a.com', port: '80' }];
    expect(field(() => buildSave(draft({ host: 'A.com', port: '80', addresses: rows }), 'manual', null))).toBe('addresses');
  });

  it('marks the bad address rows', () => {
    const rows = [
      { key: 'a', id: 'a', name: '', host: '', port: '70000' },
      { key: 'b', id: 'b', name: '', host: 'b.com', port: '1' },
      { key: 'c', id: 'c', name: '', host: 'B.com', port: '1' },
    ];
    const problems = checkAddresses(draft({ addresses: rows }), null);
    expect(Object.keys(problems)).toEqual(['a', 'c']);
    expect(problems.a.host && problems.a.port).toBeTruthy();
  });
});
