// Client-side checks for the settings page; they mirror the server rules so mistakes show before a request is sent.
import { FieldError } from '$lib/form.svelte';

export const OFFLINE_MIN = 30;
export const OFFLINE_MAX = 3600;
export const OFFLINE_DEFAULT = 60;

/** The public URL as the server will store it: trimmed, without trailing slashes. */
export const normaliseUrl = (text: string) => text.trim().replace(/\/+$/, '');

const URL_RE = /^https?:\/\/(?:[A-Za-z0-9.-]+|\[[0-9a-fA-F:]+\])(?::[0-9]{1,5})?$/;

export function checkPublicUrl(text: string): string {
  const url = normaliseUrl(text);
  if (url && !URL_RE.test(url)) throw new FieldError('格式应为 http(s)://域名或IP[:端口]，不带路径', 'public_url');
  return url;
}

export function checkOfflineSeconds(text: string): number {
  const value = text.trim();
  if (!/^[0-9]{1,5}$/.test(value) || Number(value) < OFFLINE_MIN || Number(value) > OFFLINE_MAX)
    throw new FieldError(`失联等待时间应为 ${OFFLINE_MIN}～${OFFLINE_MAX} 秒的整数`, 'failover_offline_seconds');
  return Number(value);
}

export interface AccountDraft {
  current: string;
  username: string;
  password: string;
  repeat: string;
}

/** Validate the account form and build the request body; an empty new password keeps the current one. */
export function checkAccount(draft: AccountDraft): { current_password: string; username: string; password: string } {
  if (!draft.current) throw new FieldError('请输入当前密码', 'current_password');
  const username = draft.username.trim();
  if (!/^[A-Za-z0-9_.@-]{1,32}$/.test(username)) throw new FieldError('用户名为 1-32 位字母、数字或 _ . @ -', 'username');
  if (draft.password && (draft.password.length < 8 || draft.password.length > 128)) throw new FieldError('密码长度为 8-128 位', 'password');
  if (draft.password !== draft.repeat) throw new FieldError('两次输入的新密码不一致', 'repeat');
  return { current_password: draft.current, username, password: draft.password };
}

/** "1 分钟", "1 分 30 秒", "45 秒": a readable form of a seconds value. */
export function duration(seconds: number): string {
  const minutes = Math.floor(seconds / 60);
  const rest = seconds % 60;
  if (!minutes) return `${rest} 秒`;
  return rest ? `${minutes} 分 ${rest} 秒` : `${minutes} 分钟`;
}
