// Pure helpers for the node pages: install command, address rows, validation and result formatting.
import { dateTime, shellQuote } from '$lib/format';
import type { MetricWindow, NodeAddress, NodeDetail } from '$lib/types';

type Node = NodeDetail['node'];
type Network = Node['network'];

/** Address the agent syncs with: the configured public URL, else wherever the panel is open. */
export const syncUrl = (publicUrl: string, syncPath: string, origin: string) => (publicUrl || origin) + syncPath;

export const KEY_MASK = '••••••';

/** One-line installer; the template is part of the product contract, keep it byte for byte. */
export function installCommand(installerUrl: string, url: string, key: string, mode: string): string {
  const kind = installerUrl ? ` --kind ${shellQuote(mode)}` : '';
  return `(f=$(mktemp) && trap 'rm -f "$f"' EXIT && curl -fsSL ${shellQuote(installerUrl)} -o "$f" && sudo bash "$f" --url ${shellQuote(url)} --key ${shellQuote(key)}${kind})`;
}

export const AGENT_UPDATE_TEXT: Partial<Record<Node['agent_update'], string>> = {
  pending: '等待节点自动更新',
  disabled: '节点已关闭自动更新，可在节点上运行 sudo ictl --update 更新',
  manual: '请在节点上运行 sudo ictl --update 更新被动 Agent',
  switch: '请在节点上运行 sudo ictl --sync 确认安装模式',
  newer: '节点端版本高于目标版本',
};

export const installed = (node: Pick<Node, 'installed_agent'>) => !!node.installed_agent && node.installed_agent !== 'unknown';

// ---- address rows -------------------------------------------------------------------------------

export interface AddressRow {
  id: string;
  primary: boolean;
  host: string;
  name: string;
  local: string;
  device: string;
  gateway: string;
}
export type RowErrors = Partial<Record<'host' | 'name' | 'local' | 'device' | 'gateway', string>>;
export const MAX_SECONDARY = 32;

export function newRowId(): string {
  // crypto.randomUUID is missing on plain-HTTP panels; getRandomValues is not.
  const bytes = crypto.getRandomValues(new Uint8Array(8));
  return 'ip-' + [...bytes].map((b) => b.toString(16).padStart(2, '0')).join('');
}

export const emptyRow = (primary = false): AddressRow => ({ id: newRowId(), primary, host: '', name: '', local: '', device: '', gateway: '' });

/** Editable copy of a node's addresses; a source IP equal to the address itself is the default and shown empty. */
export function initialRows(node: Pick<Node, 'addr' | 'addresses'> | null | undefined): AddressRow[] {
  const rows: AddressRow[] = (node?.addresses ?? []).map((a: NodeAddress) => ({
    id: a.id,
    primary: !!a.primary,
    host: a.host ?? '',
    name: a.name ?? '',
    local: a.local && a.local !== a.host ? a.local : '',
    device: a.device ?? '',
    gateway: a.gateway ?? '',
  }));
  if (!rows.length) rows.push({ ...emptyRow(true), host: node?.addr ?? '' });
  const first = rows.findIndex((row) => row.primary);
  return rows.map((row, index) => ({ ...row, primary: index === Math.max(first, 0) }));
}

/** The NIC that carries `ip`, when the inventory is complete and exactly one does. */
export function deviceFor(ip: string, network: Network | null | undefined): string {
  if (!network?.complete || !ip) return '';
  const devices = [...new Set(network.addresses.filter((a) => a.local === ip && a.device).map((a) => a.device))];
  return devices.length === 1 ? devices[0] : '';
}

/** Fill empty NIC fields from the inventory. Returns the same array when nothing changed. */
export function autofillDevices(rows: AddressRow[], network: Network | null | undefined): AddressRow[] {
  let changed = false;
  const next = rows.map((row) => {
    if (row.device.trim()) return row;
    const device = deviceFor(row.local.trim() || row.host.trim(), network);
    if (!device) return row;
    changed = true;
    return { ...row, device };
  });
  return changed ? next : rows;
}

const IPV4 = /^(25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)(\.(25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)){3}$/;
export const isIPv4 = (value: string) => IPV4.test(value);
/** IPv4 a peer can actually connect to: not unspecified, loopback or multicast. */
export function usableIPv4(value: string): boolean {
  if (!isIPv4(value)) return false;
  const first = Number(value.split('.')[0]);
  return value !== '0.0.0.0' && first !== 0 && first !== 127 && first < 224;
}
const DOMAIN = /^(?=.{1,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z][a-z0-9-]{0,61}[a-z0-9]$/i;
const DEVICE = /^[A-Za-z0-9_.:-]{1,15}$/;

/**
 * Cheap mirror of the server's address rules. `original` are the stored addresses: an unchanged
 * secondary host is accepted as is, like the server does. The server stays the authority.
 */
export function validateRows(rows: AddressRow[], original: NodeAddress[] = []): { rows: Record<string, RowErrors>; list: string } {
  const out: Record<string, RowErrors> = {};
  const stored = new Map(original.map((a) => [a.id, a.host]));
  let list = '';
  if (rows.filter((row) => !row.primary).length > MAX_SECONDARY) list = `每个节点最多配置 ${MAX_SECONDARY} 个副 IP`;
  if (rows.filter((row) => row.primary).length !== 1) list = '请选择一个主 IP';
  for (const row of rows) {
    const errors: RowErrors = {};
    const host = row.host.trim();
    if (!host) errors.host = '请填写地址';
    else if (/\s/.test(host)) errors.host = '地址不能包含空格';
    else if (row.primary) {
      if (/^[\d.]+$/.test(host) ? !usableIPv4(host) : !DOMAIN.test(host)) errors.host = '请填写有效的 IPv4 地址或域名';
    } else if (stored.get(row.id) !== host && !usableIPv4(host)) errors.host = '副 IP 须为可用的 IPv4 地址';
    if (row.name.trim().length > 64) errors.name = '名称最长 64 个字符';
    if (row.local.trim() && !isIPv4(row.local.trim())) errors.local = '请填写 IPv4 地址';
    if (row.gateway.trim() && !isIPv4(row.gateway.trim())) errors.gateway = '请填写 IPv4 地址';
    if (row.device.trim() && !DEVICE.test(row.device.trim())) errors.device = '网卡名称最长 15 个字符，仅限字母、数字和 _ . : -';
    if (Object.keys(errors).length) out[row.id] = errors;
  }
  return { rows: out, list };
}

export const isPort = (value: unknown, allowZero = true) =>
  typeof value === 'number' && Number.isInteger(value) && value >= (allowZero ? 0 : 1) && value <= 65535;

// ---- telemetry ----------------------------------------------------------------------------------

export interface HistorySlot {
  time: number;
  cpu: number | null;
  memory: number | null;
  samples: number;
}

/** The last `minutes` one-minute windows ending at `now`; minutes without samples stay empty rather than zero. */
export function recentHistory(history: MetricWindow[], now: number, minutes = 20): HistorySlot[] {
  const end = Math.floor(now / 60) * 60;
  const byTime = new Map(history.map((w) => [Math.floor(w.time / 60) * 60, w]));
  const num = (v: unknown) => (typeof v === 'number' && Number.isFinite(v) ? v : null);
  return Array.from({ length: minutes }, (_, i) => {
    const time = end - (minutes - 1 - i) * 60;
    const data = byTime.get(time)?.data;
    return {
      time,
      cpu: num(data?.values?.cpu_percent?.mean),
      memory: num(data?.values?.memory_used_percent?.mean),
      samples: num(data?.samples) ?? 0,
    };
  });
}

/** Share used, 0–100, from decimal-string or numeric totals; null when unknown. */
export function usedPercent(available: unknown, total: unknown): number | null {
  const a = Number(available);
  const t = Number(total);
  if (available === null || available === undefined || available === '' || !Number.isFinite(a) || !Number.isFinite(t) || t <= 0) return null;
  return Math.min(100, Math.max(0, ((t - a) / t) * 100));
}

// ---- diagnostics --------------------------------------------------------------------------------

export const QUERY_KINDS = [
  { value: 'inventory', label: '系统信息' },
  { value: 'ports', label: '端口快照' },
  { value: 'port_detail', label: '端口详情（含防火墙规则导出）' },
  { value: 'network', label: '网络信息' },
  { value: 'rules', label: '当前规则' },
  { value: 'logs', label: '最近日志' },
  { value: 'diagnose', label: '转发诊断' },
  { value: 'connections', label: '转发连接' },
] as const;
export type QueryKind = (typeof QUERY_KINDS)[number]['value'];
export const needsRule = (kind: string) => kind === 'diagnose' || kind === 'connections';

const LABELS: Record<string, string> = {
  sampled_at: '采样时间',
  kind: '检查项目',
  note: '说明',
  dns_ms: 'DNS 解析耗时 (ms)',
  tcp_connect_ms: 'TCP 连接耗时 (ms)',
  resolved_ip: '解析到的 IP',
  reachable: '可达',
  addresses: '地址',
  routes: '路由',
  dns: 'DNS',
  rules: '规则',
  logs: '日志',
  connections: '连接',
  error: '错误',
};

function block(value: unknown): string {
  if (typeof value === 'string') {
    const text = value.trim();
    if (/^[[{]/.test(text)) {
      try {
        return JSON.stringify(JSON.parse(text), null, 2);
      } catch {
        // not JSON after all: show the raw text
      }
    }
    return value.replace(/\s+$/, '');
  }
  return JSON.stringify(value, null, 2);
}

/** Readable text for a query result: short values as a header, long text and structures as titled sections. */
export function formatQueryResult(data: unknown): string {
  if (data === null || typeof data !== 'object' || Array.isArray(data)) return block(data);
  const head: string[] = [];
  const sections: string[] = [];
  for (const [key, value] of Object.entries(data as Record<string, unknown>)) {
    const label = LABELS[key] ? `${LABELS[key]} (${key})` : key;
    if (value === null || value === undefined || value === '') continue;
    if (key === 'sampled_at' && typeof value === 'number') head.push(`${label}: ${dateTime(Math.floor(value / 1000))}`);
    else if (typeof value === 'number' || typeof value === 'boolean') head.push(`${label}: ${value}`);
    else if (typeof value === 'string' && value.length <= 120 && !value.includes('\n') && !/^\s*[[{]/.test(value)) head.push(`${label}: ${value}`);
    else if (Array.isArray(value) && value.length <= 8 && value.every((v) => typeof v === 'string' || typeof v === 'number'))
      head.push(`${label}: ${value.join(', ') || '-'}`);
    else if (typeof value === 'object' && !Array.isArray(value) && Object.values(value as object).every((v) => typeof v === 'string'))
      for (const [sub, text] of Object.entries(value as Record<string, string>)) sections.push(`── ${label} · ${sub} ──\n${block(text) || '（空）'}`);
    else sections.push(`── ${label} ──\n${block(value) || '（空）'}`);
  }
  return [head.join('\n'), ...sections].filter(Boolean).join('\n\n');
}
