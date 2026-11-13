// Display formatting shared by every page. Times are epoch seconds from the server.

const pad = (n: number) => String(n).padStart(2, '0');

/** Local "HH:MM:SS". */
export function clock(ts: number): string {
  const d = new Date(ts * 1000);
  return `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
}

/** Local "YYYY-MM-DD HH:MM:SS", or "-" when unset. */
export function dateTime(ts: number | null | undefined): string {
  if (!ts) return '-';
  const d = new Date(ts * 1000);
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${clock(ts)}`;
}

/** Relative time against the server clock, so a skewed browser clock does not matter. */
export function ago(ts: number | null | undefined, now: number, never = '从未上报'): string {
  if (!ts) return never;
  const d = Math.max(0, now - ts);
  if (d < 60) return `${Math.floor(d)} 秒前`;
  if (d < 3600) return `${Math.floor(d / 60)} 分钟前`;
  if (d < 86400) return `${Math.floor(d / 3600)} 小时前`;
  return `${Math.floor(d / 86400)} 天前`;
}

const UNITS = ['B', 'KiB', 'MiB', 'GiB', 'TiB', 'PiB'];

/** Byte counts arrive as numbers or decimal strings beyond 2^53; two decimals, truncated. */
export function bytes(value: number | string | bigint | null | undefined): string {
  if (value === null || value === undefined || value === '') return '-';
  let n: bigint;
  try {
    n = typeof value === 'number' ? BigInt(Math.trunc(value)) : BigInt(value);
  } catch {
    return '-';
  }
  if (n < 0n) return '-';
  let unit = 0;
  let scale = 1n;
  while (unit < UNITS.length - 1 && n >= scale * 1024n) {
    scale *= 1024n;
    unit++;
  }
  const hundredths = (n * 100n) / scale;
  return `${hundredths / 100n}.${String(hundredths % 100n).padStart(2, '0')} ${UNITS[unit]}`;
}

export const rate = (value: number | null | undefined) =>
  value === null || value === undefined ? '-' : bytes(Math.round(value)) + '/s';

export const percent = (value: number | null | undefined) =>
  value === null || value === undefined || Number.isNaN(Number(value)) ? '-' : Number(value).toFixed(1) + '%';

/** "+5" for positive numbers, plain otherwise. */
export const signed = (value: number) => (value > 0 ? `+${value}` : String(value));

/** Case-insensitive substring match on a trimmed query; an empty query matches everything. */
export function matches(text: string | null | undefined, query: string): boolean {
  const q = query.trim().toLowerCase();
  return !q || (text ?? '').toLowerCase().includes(q);
}

/** Quote a value for a POSIX shell command line. */
export const shellQuote = (value: string) => `'${value.replace(/'/g, `'\\''`)}'`;
