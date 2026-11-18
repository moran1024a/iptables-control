import { api, ApiError, onUnauthorized } from './api';

export type SessionStatus = 'loading' | 'offline' | 'anonymous' | 'must-change' | 'ready';

class Session {
  status = $state<SessionStatus>('loading');
  username = $state('');
  admin = $state(false);
  version = $state('');
  error = $state('');
  private changes = typeof window !== 'undefined' && typeof BroadcastChannel !== 'undefined'
    ? new BroadcastChannel('ictl-session:' + window.location.pathname)
    : null;

  constructor() {
    if (this.changes) this.changes.onmessage = () => location.reload();
    onUnauthorized((mustChange) => {
      this.status = mustChange ? 'must-change' : 'anonymous';
    });
  }

  async load() {
    try {
      const me = await api<{ username: string; version: string; must_change: boolean; admin: boolean }>('GET', 'me', undefined, { quiet: true });
      this.username = me.username;
      this.admin = me.admin;
      this.version = me.version;
      this.status = me.must_change ? 'must-change' : 'ready';
    } catch (err) {
      if (err instanceof ApiError && err.status === 401) this.status = 'anonymous';
      else {
        this.error = err instanceof Error ? err.message : String(err);
        this.status = 'offline';
      }
    }
  }

  async login(username: string, password: string) {
    await api('POST', 'login', { username, password }, { quiet: true });
    this.changes?.postMessage('changed');
    await this.load();
  }

  async loginAsChild(id: number, revision: number) {
    await api('POST', `accounts/${id}/login`, { revision });
    this.changes?.postMessage('changed');
    history.replaceState(null, '', '#/');
    location.reload();
  }

  async logout() {
    try {
      await api('POST', 'logout', {}, { quiet: true });
      this.changes?.postMessage('changed');
    } finally {
      this.status = 'anonymous';
    }
  }

  /** The server keeps this session and drops the account's other sessions. */
  async changeAccount(body: { current_password: string; username: string; password: string }) {
    const result = await api<{ username: string }>('POST', 'account', body, { quiet: true });
    this.username = result.username;
    if (this.status === 'must-change') this.status = 'ready';
  }
}

export const session = new Session();
