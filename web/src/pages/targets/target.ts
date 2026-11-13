// Pure helpers for the targets page: labels, the address editor rows and the save payload.
import { FieldError } from '$lib/form.svelte';
import type { Target, TargetAddressInput, TargetSave } from '$lib/types';

export type TargetMode = 'manual' | 'link';

/** One editable extra address; `key` is stable for the list, `id` is what the server stores. */
export interface AddressRow {
  key: string;
  id: string;
  name: string;
  host: string;
  port: string;
}

export interface TargetDraft {
  name: string;
  /** '' = leave the group to the server (the default group on create). */
  group: string;
  host: string;
  port: string;
  uri: string;
  addresses: AddressRow[];
}

export const MAX_ADDRESSES = 32;
const PORT_MESSAGE = '端口必须是 1-65535 的整数';

/** Short protocol label for the row badge. */
export function typeLabel(target: Pick<Target, 'type' | 'params'>): string {
  if (target.type === 'endpoint') return '地址';
  if (target.type === 'vless-enc') return 'VLESS';
  if (target.type === 'shadowsocks') {
    const method = 'method' in target.params ? target.params.method : '';
    return method.startsWith('2022-') ? 'SS2022' : 'SS';
  }
  return target.type;
}

/** Full protocol name for tooltips and the edit dialog. */
export function typeName(target: Pick<Target, 'type' | 'params'>): string {
  return { 地址: 'IP／域名＋端口', SS: 'Shadowsocks', SS2022: 'Shadowsocks 2022', VLESS: 'VLESS ENC' }[typeLabel(target)] ?? target.type;
}

export const isImported = (target: Pick<Target, 'type'>) => target.type !== 'endpoint';

/** What "copy" puts on the clipboard: the share link, or `host:port` for a plain endpoint. */
export const copyValue = (target: Pick<Target, 'type' | 'uri' | 'host' | 'port'>) =>
  isImported(target) && target.uri ? target.uri : `${target.host}:${target.port}`;

/** Address ids only need to be unique inside one target; works on plain-HTTP panels where `randomUUID` is missing. */
export function newAddressId(): string {
  const bytes = crypto.getRandomValues(new Uint8Array(8));
  return 'a-' + [...bytes].map((byte) => byte.toString(16).padStart(2, '0')).join('');
}

export const emptyRow = (): AddressRow => {
  const id = newAddressId();
  return { key: id, id, name: '', host: '', port: '' };
};

export function draftFor(target: Target | null, defaultGroup: string): TargetDraft {
  if (!target) return { name: '', group: defaultGroup, host: '', port: '', uri: '', addresses: [] };
  return {
    name: target.name,
    group: String(target.group_id),
    host: target.host,
    port: String(target.port),
    uri: isImported(target) ? target.uri : '',
    addresses: target.addresses
      .filter((address) => address.id !== 'main')
      .map((address) => ({ key: address.id, id: address.id, name: address.name, host: address.host, port: String(address.port) })),
  };
}

/** A port typed by the user, or null when it is not an integer in 1..65535. */
export function parsePort(text: string): number | null {
  const value = text.trim();
  return /^[1-9][0-9]{0,4}$/.test(value) && Number(value) <= 65535 ? Number(value) : null;
}

/** Problems in the extra-address rows, keyed by row key, so the editor can mark the exact input. */
export type RowProblems = Record<string, { host?: string; port?: string }>;

export function checkAddresses(draft: TargetDraft, main: { host: string; port: number | null } | null): RowProblems {
  const problems: RowProblems = {};
  const seen = new Set<string>();
  if (main && main.host && main.port !== null) seen.add(`${main.host.toLowerCase()}:${main.port}`);
  for (const row of draft.addresses) {
    const problem: { host?: string; port?: string } = {};
    const host = row.host.trim();
    const port = parsePort(row.port);
    if (!host) problem.host = '请填写 IPv4 地址或域名';
    if (port === null) problem.port = PORT_MESSAGE;
    if (host && port !== null) {
      const key = `${host.toLowerCase()}:${port}`;
      if (seen.has(key)) problem.host = '与其他接入地址重复';
      seen.add(key);
    }
    if (problem.host || problem.port) problems[row.key] = problem;
  }
  return problems;
}

/**
 * Validate the draft and build the request body. Throws `FieldError` for the first problem.
 * On edit of an imported target the link is only sent when it was changed, so a rename never re-imports.
 */
export function buildSave(draft: TargetDraft, mode: TargetMode, target: Target | null): TargetSave {
  const name = draft.name.trim();
  if (target && !name) throw new FieldError('请填写名称', 'name');
  if ([...name].length > 64) throw new FieldError('名称最长 64 个字符', 'name');

  let main: { host: string; port: number | null } | null = null;
  let connection: { uri: string } | { host: string; port: number } | Record<string, never> = {};
  if (mode === 'manual') {
    const host = draft.host.trim();
    if (!host) throw new FieldError('请填写 IPv4 地址或域名', 'host');
    const port = parsePort(draft.port);
    if (port === null) throw new FieldError(PORT_MESSAGE, 'port');
    main = { host, port };
    connection = { host, port };
  } else {
    const uri = draft.uri.trim();
    if (!uri) throw new FieldError('请粘贴分享链接', 'uri');
    if (uri.length > 8192) throw new FieldError('分享链接最长 8192 个字符', 'uri');
    if (!target || uri !== target.uri) connection = { uri };
    else main = { host: target.host, port: target.port };
  }

  if (draft.addresses.length > MAX_ADDRESSES) throw new FieldError(`每个目标最多添加 ${MAX_ADDRESSES} 个接入地址`, 'addresses');
  const problems = checkAddresses(draft, main);
  const firstBad = draft.addresses.findIndex((row) => problems[row.key]);
  if (firstBad !== -1) {
    const problem = problems[draft.addresses[firstBad].key];
    throw new FieldError(`第 ${firstBad + 1} 个接入地址：${problem.host ?? problem.port}`, 'addresses');
  }
  const addresses: TargetAddressInput[] = draft.addresses.map((row) => ({
    id: row.id,
    name: row.name.trim(),
    host: row.host.trim(),
    port: parsePort(row.port)!,
  }));

  const body: Record<string, unknown> = { ...connection, addresses };
  if (name) body.name = name;
  if (draft.group) body.group_id = Number(draft.group);
  return body as TargetSave;
}
