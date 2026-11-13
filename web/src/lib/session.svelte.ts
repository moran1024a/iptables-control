import { api, ApiError, onUnauthorized } from './api';

export type SessionStatus = 'loading' | 'offline' | 'anonymous' | 'must-change' | 'ready';

class Session {
  status = $state<SessionStatus>('loading');
  username = $state('');
  version = $state('');
  error = $state('');

  constructor() {
    onUnauthorized((mustChange) => {
      this.status = mustChange ? 'must-change' : 'anonymous';
    });
  }

  async load() {
    try {
      const me = await api<{ username: string; version: string; must_change: boolean }>('GET', 'me', undefined, { quiet: true });
      this.username = me.username;
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
    await this.load();
  }

  async logout() {
    try {
      await api('POST', 'logout', {}, { quiet: true });
    } finally {
      this.status = 'anonymous';
    }
  }

  /** Change the administrator account; the server keeps this session and drops the others. */
  async changeAccount(body: { current_password: string; username: string; password: string }) {
    const result = await api<{ username: string }>('POST', 'account', body, { quiet: true });
    this.username = result.username;
    if (this.status === 'must-change') this.status = 'ready';
  }
}

export const session = new Session();
