export interface ConfirmRequest {
  title: string;
  body?: string;
  /** Text the user must be able to read in full, shown in a monospace block. */
  detail?: string;
  confirmLabel?: string;
  cancelLabel?: string;
  danger?: boolean;
}

class Confirm {
  current = $state<(ConfirmRequest & { resolve: (ok: boolean) => void }) | null>(null);

  ask(request: ConfirmRequest): Promise<boolean> {
    this.current?.resolve(false);
    return new Promise((resolve) => {
      this.current = { ...request, resolve };
    });
  }

  answer(ok: boolean) {
    const pending = this.current;
    this.current = null;
    pending?.resolve(ok);
  }
}

export const confirmState = new Confirm();
export const confirm = (request: ConfirmRequest) => confirmState.ask(request);
