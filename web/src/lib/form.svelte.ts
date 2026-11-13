import { ApiError, errorMessage } from './api';

/**
 * Submission state for one form: busy flag and server errors routed to the field they name.
 * Errors for fields the form does not render fall back to the form-level message.
 */
export class FormState {
  busy = $state(false);
  errors = $state<Record<string, string>>({});
  error = $state('');
  /** HTTP status of the last failure, e.g. 409 for a revision conflict. */
  status = $state(0);

  constructor(private fields: string[] = []) {}

  clear() {
    this.errors = {};
    this.error = '';
    this.status = 0;
  }

  fail(message: string, field?: string) {
    if (field && this.fields.includes(field)) this.errors = { ...this.errors, [field]: message };
    else this.error = message;
  }

  /** Run a submission. Returns true on success; failures are recorded, never thrown. */
  async run(action: () => Promise<unknown>): Promise<boolean> {
    if (this.busy) return false;
    this.clear();
    this.busy = true;
    try {
      await action();
      return true;
    } catch (err) {
      if (err instanceof ApiError) {
        // The session store already switched to the sign-in page.
        if (err.status === 401 || (err.status === 403 && err.data.must_change)) return false;
        this.status = err.status;
        this.fail(err.message, err.field);
      } else if (err instanceof FieldError) this.fail(err.message, err.field);
      else this.fail(errorMessage(err));
      return false;
    } finally {
      this.busy = false;
    }
  }
}

/** Client-side validation failure, routed like a server error. */
export class FieldError extends Error {
  constructor(
    message: string,
    public field?: string,
  ) {
    super(message);
  }
}

/** Stable serialisation for dirty checks. */
export const snapshot = (value: unknown) => JSON.stringify(value);
