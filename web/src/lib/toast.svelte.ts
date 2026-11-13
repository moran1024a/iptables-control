export type ToastTone = 'ok' | 'error' | 'info';
export interface Toast {
  id: number;
  tone: ToastTone;
  text: string;
}

let seq = 0;

class Toasts {
  items = $state<Toast[]>([]);

  push(text: string, tone: ToastTone = 'info', duration = tone === 'error' ? 6000 : 2600) {
    const id = ++seq;
    // The same message repeated by a retry or a poll replaces the old one.
    this.items = [...this.items.filter((t) => t.text !== text), { id, tone, text }].slice(-4);
    setTimeout(() => this.dismiss(id), duration);
  }

  dismiss(id: number) {
    this.items = this.items.filter((t) => t.id !== id);
  }
}

export const toasts = new Toasts();
export const toast = {
  ok: (text: string) => toasts.push(text, 'ok'),
  error: (text: string) => toasts.push(text, 'error'),
  info: (text: string) => toasts.push(text, 'info'),
};
