import { describe, expect, it } from 'vitest';
import {
  autofillDevices,
  formatQueryResult,
  initialRows,
  installCommand,
  recentHistory,
  syncUrl,
  usableIPv4,
  usedPercent,
  validateRows,
  type AddressRow,
} from './node-logic';

const row = (over: Partial<AddressRow>): AddressRow => ({ id: 'a', primary: false, host: '', name: '', local: '', device: '', gateway: '', ...over });

describe('install command', () => {
  it('matches the product template and quotes values', () => {
    expect(installCommand('https://x/install-agent.sh', "https://p/s'x", 'k', 'passive')).toBe(
      `(f=$(mktemp) && trap 'rm -f "$f"' EXIT && curl -fsSL 'https://x/install-agent.sh' -o "$f" && sudo bash "$f" --url 'https://p/s'\\''x' --key 'k' --kind 'passive')`,
    );
  });
  it('falls back to the page origin', () => {
    expect(syncUrl('', '/abc', 'http://h:1')).toBe('http://h:1/abc');
    expect(syncUrl('https://pub', '/abc', 'http://h:1')).toBe('https://pub/abc');
  });
});

describe('address rows', () => {
  it('creates a primary row for a new node and hides a default source IP', () => {
    expect(initialRows(null)).toMatchObject([{ primary: true, host: '' }]);
    const rows = initialRows({
      addr: '1.1.1.1',
      addresses: [
        { id: 'main', primary: false, name: '', host: '1.1.1.1', local: '1.1.1.1', device: 'eth0', gateway: '' },
        { id: 'b', primary: false, name: 'x', host: '2.2.2.2', local: '10.0.0.2', device: '', gateway: '' },
      ],
    });
    expect(rows.map((r) => [r.primary, r.local])).toEqual([[true, ''], [false, '10.0.0.2']]);
  });
  it('auto-fills the NIC only from a complete inventory with a single match', () => {
    const network = { complete: true, sampled_at: 1, addresses: [{ local: '10.0.0.2', device: 'eth1' }, { local: '9.9.9.9', device: 'a' }, { local: '9.9.9.9', device: 'b' }] };
    const rows = [row({ id: '1', host: '2.2.2.2', local: '10.0.0.2' }), row({ id: '2', host: '9.9.9.9' }), row({ id: '3', host: '10.0.0.2', device: 'keep' })];
    expect(autofillDevices(rows, network).map((r) => r.device)).toEqual(['eth1', '', 'keep']);
    expect(autofillDevices(rows, { ...network, complete: false })).toBe(rows);
  });
  it('validates like the server', () => {
    expect(usableIPv4('127.0.0.1')).toBe(false);
    expect(usableIPv4('224.0.0.1')).toBe(false);
    expect(usableIPv4('192.0.2.1')).toBe(true);
    const rows = [
      row({ id: 'p', primary: true, host: 'de.example.com' }),
      row({ id: 's', host: 'example.com' }),
      row({ id: 'kept', host: 'old.example.com' }),
      row({ id: 'adv', host: '192.0.2.9', local: 'x', device: 'a b', gateway: '1.2.3' }),
    ];
    const original = [{ id: 'kept', primary: false, name: '', host: 'old.example.com', local: '', device: '', gateway: '' }];
    const result = validateRows(rows, original);
    expect(result.list).toBe('');
    expect(Object.keys(result.rows)).toEqual(['s', 'adv']);
    expect(Object.keys(result.rows.adv)).toEqual(['local', 'gateway', 'device']);
    expect(validateRows([row({ primary: true, host: '300.1.1.1' })]).rows.a.host).toBeTruthy();
    expect(validateRows([row({ host: '1.1.1.1' })]).list).toBe('请选择一个主 IP');
  });
});

describe('telemetry', () => {
  it('keeps missing minutes empty', () => {
    const win = (time: number, cpu: number) => ({ time, data: { samples: 3, first_at: time, last_at: time, values: { cpu_percent: { count: 3, sum: 0, min: 0, max: 0, mean: cpu } } } });
    const slots = recentHistory([win(600, 5), win(480, 7), win(0, 99)], 650, 4);
    expect(slots.map((s) => [s.time, s.cpu, s.samples])).toEqual([[420, null, 0], [480, 7, 3], [540, null, 0], [600, 5, 3]]);
  });
  it('computes usage', () => {
    expect(usedPercent('25', '100')).toBe(75);
    expect(usedPercent(undefined, 100)).toBeNull();
    expect(usedPercent(1, 0)).toBeNull();
  });
});

describe('query result', () => {
  it('lists short values first and pretty-prints embedded JSON', () => {
    const text = formatQueryResult({ kind: 'network', dns: 'nameserver 1.1.1.1\nnameserver 8.8.8.8\n', routes: '[{"dst":"default"}]', reachable: true });
    expect(text).toContain('检查项目 (kind): network');
    expect(text).toContain('可达 (reachable): true');
    expect(text).toContain('── DNS (dns) ──\nnameserver 1.1.1.1\nnameserver 8.8.8.8');
    expect(text).toContain('"dst": "default"');
  });
});
