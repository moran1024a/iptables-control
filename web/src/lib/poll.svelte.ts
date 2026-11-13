import { errorMessage } from './api';
import { overlays } from './overlay.svelte';

export interface PollOptions {
  /** Milliseconds between refreshes; 0 loads once. */
  interval?: number;
  /** Return true to skip a scheduled refresh. Open dialogs always pause polling. */
  paused?: () => boolean;
  /** Keep polling while a dialog is open, for data shown inside the dialog itself. */
  duringDialogs?: boolean;
}

/**
 * A resource that reloads itself while the component using it is mounted.
 * Late responses from an earlier request are dropped, a hidden tab stops polling,
 * and the previous data stays on screen while a refresh or an error happens.
 */
export class Poll<T> {
  data = $state.raw<T | undefined>(undefined);
  error = $state<string | null>(null);
  loading = $state(true);
  /** Epoch ms of the last successful load. */
  loadedAt = $state(0);

  private request = 0;
  private timer: ReturnType<typeof setTimeout> | undefined;
  private controller: AbortController | undefined;
  private active = false;

  constructor(
    private fetcher: (signal: AbortSignal) => Promise<T>,
    private options: PollOptions = {},
  ) {}

  /** Call from `$effect`; returns the cleanup. Re-running restarts with fresh inputs. */
  start = () => {
    this.active = true;
    const visible = () => {
      if (!document.hidden && this.active) void this.refresh();
    };
    document.addEventListener('visibilitychange', visible);
    void this.refresh();
    return () => {
      this.active = false;
      this.request++;
      clearTimeout(this.timer);
      this.controller?.abort();
      document.removeEventListener('visibilitychange', visible);
    };
  };

  refresh = async (): Promise<void> => {
    if (!this.active) return;
    const request = ++this.request;
    clearTimeout(this.timer);
    this.controller?.abort();
    const controller = (this.controller = new AbortController());
    try {
      const data = await this.fetcher(controller.signal);
      if (request !== this.request) return;
      this.data = data;
      this.error = null;
      this.loadedAt = Date.now();
    } catch (err) {
      if (request !== this.request || controller.signal.aborted) return;
      this.error = errorMessage(err);
    } finally {
      if (request === this.request) {
        this.loading = false;
        this.schedule();
      }
    }
  };

  private schedule() {
    const interval = this.options.interval ?? 0;
    if (!interval || !this.active) return;
    this.timer = setTimeout(() => {
      const waiting = document.hidden || this.options.paused?.() || (overlays.open && !this.options.duringDialogs);
      if (waiting) this.schedule();
      else void this.refresh();
    }, interval);
  }
}

/** Create a poll bound to the calling component's lifetime. */
export function usePoll<T>(fetcher: (signal: AbortSignal) => Promise<T>, options: PollOptions = {}) {
  const poll = new Poll(fetcher, options);
  $effect(() => poll.start());
  return poll;
}
