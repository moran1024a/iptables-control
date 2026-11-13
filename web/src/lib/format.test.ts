import { describe, expect, it } from 'vitest';
import { ago, bytes, matches, percent, rate, shellQuote, signed } from './format';

describe('bytes', () => {
  it('truncates to two decimals with binary units', () => {
    expect(bytes(0)).toBe('0.00 B');
    expect(bytes(1023)).toBe('1023.00 B');
    expect(bytes(1536)).toBe('1.50 KiB');
    expect(bytes(1024 * 1024 - 1)).toBe('1023.99 KiB');
    expect(bytes('18446744073709551615')).toBe('16383.99 PiB');
  });
  it('shows a dash for missing or malformed counters', () => {
    for (const value of [null, undefined, '', 'abc', -1]) expect(bytes(value as never)).toBe('-');
  });
});

describe('small formatters', () => {
  it('formats rates, percentages and signs', () => {
    expect(rate(1536.4)).toBe('1.50 KiB/s');
    expect(rate(null)).toBe('-');
    expect(percent(12.345)).toBe('12.3%');
    expect(percent(undefined)).toBe('-');
    expect(signed(5)).toBe('+5');
    expect(signed(0)).toBe('0');
    expect(signed(-3)).toBe('-3');
  });
  it('describes elapsed time against the server clock', () => {
    expect(ago(null, 100)).toBe('从未上报');
    expect(ago(100, 90)).toBe('0 秒前');
    expect(ago(100, 159)).toBe('59 秒前');
    expect(ago(100, 160)).toBe('1 分钟前');
    expect(ago(100, 100 + 7200)).toBe('2 小时前');
    expect(ago(100, 100 + 3 * 86400)).toBe('3 天前');
  });
  it('matches case-insensitively and quotes for the shell', () => {
    expect(matches('Tokyo-01', ' tok ')).toBe(true);
    expect(matches('Tokyo-01', 'osaka')).toBe(false);
    expect(matches(null, '')).toBe(true);
    expect(shellQuote("a'b")).toBe(`'a'\\''b'`);
  });
});
