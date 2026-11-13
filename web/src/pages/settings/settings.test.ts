import { describe, expect, it } from 'vitest';
import { checkAccount, checkOfflineSeconds, checkPublicUrl, duration } from './settings';

const field = (run: () => unknown) => {
  try {
    run();
  } catch (err) {
    return (err as { field?: string }).field;
  }
  return null;
};

describe('settings', () => {
  it('normalises and checks the public URL', () => {
    expect(checkPublicUrl('  https://example.com:8443// ')).toBe('https://example.com:8443');
    expect(checkPublicUrl('http://[2001:db8::1]:80')).toBe('http://[2001:db8::1]:80');
    expect(checkPublicUrl('  ')).toBe('');
    for (const bad of ['example.com', 'https://example.com/panel', 'ftp://example.com', 'https://a b']) expect(field(() => checkPublicUrl(bad))).toBe('public_url');
  });

  it('checks the offline wait', () => {
    expect(checkOfflineSeconds(' 30 ')).toBe(30);
    expect(checkOfflineSeconds('3600')).toBe(3600);
    for (const bad of ['29', '3601', '', '60.5', 'abc', '-60']) expect(field(() => checkOfflineSeconds(bad))).toBe('failover_offline_seconds');
  });

  it('checks the account form', () => {
    const base = { current: 'old-pass', username: ' ops ', password: '', repeat: '' };
    expect(checkAccount(base)).toEqual({ current_password: 'old-pass', username: 'ops', password: '' });
    expect(field(() => checkAccount({ ...base, current: '' }))).toBe('current_password');
    expect(field(() => checkAccount({ ...base, username: 'a b' }))).toBe('username');
    expect(field(() => checkAccount({ ...base, password: 'short', repeat: 'short' }))).toBe('password');
    expect(field(() => checkAccount({ ...base, password: 'long-enough', repeat: 'long-enougH' }))).toBe('repeat');
  });

  it('formats durations', () => {
    expect([45, 60, 90, 3600].map(duration)).toEqual(['45 秒', '1 分钟', '1 分 30 秒', '60 分钟']);
  });
});
