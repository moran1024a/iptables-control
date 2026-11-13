// Thin client for the panel API. Paths are relative: the panel lives under a secret prefix.

export class ApiError extends Error {
  status: number;
  field?: string;
  data: Record<string, unknown>;
  constructor(status: number, data: Record<string, unknown>) {
    super(typeof data.error === 'string' && data.error ? data.error : `请求失败（${status}）`);
    this.status = status;
    this.field = typeof data.field === 'string' ? data.field : undefined;
    this.data = data;
  }
}

type Method = 'GET' | 'POST' | 'PATCH' | 'DELETE';

let unauthorized: (mustChange: boolean) => void = () => {};
/** The session store registers here so an expired login anywhere returns to the sign-in page. */
export function onUnauthorized(handler: (mustChange: boolean) => void) {
  unauthorized = handler;
}

export async function api<T = unknown>(
  method: Method,
  path: string,
  body?: unknown,
  options: { signal?: AbortSignal; timeout?: number; quiet?: boolean } = {},
): Promise<T> {
  const controller = new AbortController();
  const abort = () => controller.abort();
  if (options.signal?.aborted) abort();
  options.signal?.addEventListener('abort', abort, { once: true });
  let timedOut = false;
  const timer = setTimeout(() => {
    timedOut = true;
    controller.abort();
  }, options.timeout ?? 20000);
  const init: RequestInit = { method, credentials: 'same-origin', signal: controller.signal, headers: {} };
  if (method !== 'GET') {
    init.headers = { 'Content-Type': 'application/json' };
    init.body = JSON.stringify(body ?? {});
  }
  let response: Response;
  try {
    response = await fetch('api/' + path, init);
  } catch (err) {
    if (timedOut) throw new ApiError(0, { error: '请求超时，请刷新核对操作结果后再重试' });
    if (controller.signal.aborted) throw err;
    throw new ApiError(0, { error: '无法连接服务端' });
  } finally {
    clearTimeout(timer);
    options.signal?.removeEventListener('abort', abort);
  }
  let data: Record<string, unknown> = {};
  try {
    data = await response.json();
  } catch {
    if (response.ok) throw new ApiError(0, { error: '服务端响应异常，请刷新核对操作结果' });
  }
  if (response.ok) return data as T;
  if (!options.quiet) {
    if (response.status === 401) unauthorized(false);
    else if (response.status === 403 && data.must_change) unauthorized(true);
  }
  throw new ApiError(response.status, data);
}

export const get = <T>(path: string, options?: { signal?: AbortSignal; timeout?: number }) =>
  api<T>('GET', path, undefined, options);
export const post = <T>(path: string, body?: unknown, options?: { timeout?: number; quiet?: boolean }) =>
  api<T>('POST', path, body, options);
export const patch = <T>(path: string, body?: unknown) => api<T>('PATCH', path, body);
export const del = <T>(path: string) => api<T>('DELETE', path);

export function errorMessage(err: unknown): string {
  if (err instanceof ApiError) return err.message;
  if (err instanceof Error && err.name === 'AbortError') return '请求已取消';
  return err instanceof Error ? err.message : String(err);
}
