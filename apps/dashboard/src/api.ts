export class ApiError extends Error {
  constructor(public code: string, message: string, public status = 0, public details?: unknown) {
    super(message);
    this.name = 'ApiError';
  }
}

/** The origin is fixed when the UI boots. No response-provided URL is ever authenticated. */
export class OwnerApi {
  readonly origin: string;
  #token = '';
  #browserTicket = '';
  #renewing?: Promise<void>;
  onExpired?: () => void;

  constructor(origin = window.location.origin) {
    this.origin = origin;
    try { this.#browserTicket = window.sessionStorage.getItem(this.sessionKey) ?? ''; } catch { /* Tab storage is optional. */ }
  }
  private get sessionKey() { return `agentflow.browser-session.v1:${this.origin}`; }
  clear() {
    this.#token = ''; this.#browserTicket = '';
    try { window.sessionStorage.removeItem(this.sessionKey); } catch { /* No ambient cookie fallback. */ }
  }

  private url(path: string): URL {
    if (!path.startsWith('/api/v1/') || path.includes('\\')) throw new ApiError('unsafe_url', '拒绝非管理接口地址');
    const url = new URL(path, this.origin);
    if (url.origin !== this.origin || url.username || url.password || url.hash || !url.pathname.startsWith('/api/v1/')) {
      throw new ApiError('unsafe_url', '拒绝跨来源管理请求');
    }
    return url;
  }

  private async fetch(path: string, method = 'GET', payload?: unknown, key?: string,
                      signal?: AbortSignal, authenticate: boolean | string = true, allowRenew = true): Promise<Response> {
    const headers = new Headers({ Accept: 'application/json' });
    if (authenticate !== false) {
      const token = typeof authenticate === 'string' ? authenticate : this.#token;
      if (!token) throw new ApiError('unauthorized', '请通过本机启动链接打开工作台', 401);
      headers.set('Authorization', `Bearer ${token}`);
    }
    if (method !== 'GET') headers.set('Idempotency-Key', key ?? crypto.randomUUID());
    if (payload !== undefined) headers.set('Content-Type', 'application/json');
    let response: Response;
    try {
      response = await window.fetch(this.url(path), {
        method, headers, body: payload === undefined ? undefined : JSON.stringify(payload),
        credentials: 'omit', mode: 'same-origin', redirect: 'error', cache: 'no-store', signal,
        // WebKit sends Origin: null for same-origin mode + no-referrer POSTs.
        // Keep the real Origin for the server's CSRF check; cross-origin URLs
        // and redirects are already rejected, and no external referrer is sent.
        referrerPolicy: 'same-origin',
      });
    } catch (error) {
      if (signal?.aborted) throw error;
      if (error instanceof ApiError) throw error;
      throw new ApiError('connection_error', '无法连接本地服务。操作结果尚未确认，请检查服务后重试。');
    }
    if (!response.ok) {
      let value: { error?: { code?: string; message?: string; details?: unknown } } = {};
      try { value = await response.json(); } catch { /* status remains authoritative */ }
      if (response.status === 401 && authenticate === true) {
        if (allowRenew && this.#browserTicket && value.error?.code === 'unauthorized') {
          try { await this.renew(); }
          catch (error) {
            if (error instanceof ApiError && [401, 403].includes(error.status)) { this.clear(); this.onExpired?.(); }
            throw error;
          }
          // Authentication was rejected before command execution; retain the
          // same idempotency key and never retry ambiguous transport failures.
          return this.fetch(path, method, payload, headers.get('Idempotency-Key') ?? key, signal, true, false);
        }
        this.clear(); this.onExpired?.();
      }
      throw new ApiError(value.error?.code ?? `http_${response.status}`,
        value.error?.message ?? `服务返回 HTTP ${response.status}`, response.status, value.error?.details);
    }
    return response;
  }

  async exchange(code: string): Promise<void> {
    const response = await this.fetch('/api/v1/session', 'POST', { bootstrap_token: code, browser_session: true }, undefined, undefined, false);
    const result = await response.json() as { owner_token?: unknown; browser_session_token?: unknown };
    if (typeof result.owner_token !== 'string' || !result.owner_token) throw new ApiError('invalid_response', '服务未返回有效会话');
    this.#token = result.owner_token;
    if (typeof result.browser_session_token === 'string' && result.browser_session_token) {
      this.#browserTicket = result.browser_session_token;
      try { window.sessionStorage.setItem(this.sessionKey, this.#browserTicket); } catch { /* Current page still works. */ }
    }
  }

  private renew(): Promise<void> {
    if (this.#renewing) return this.#renewing;
    if (!this.#browserTicket) return Promise.reject(new ApiError('unauthorized', '请通过 agentflow start 打开工作台', 401));
    this.#renewing = this.fetch('/api/v1/session/resume', 'POST', {}, undefined, undefined, this.#browserTicket, false)
      .then(response => response.json()).then((result: { owner_token?: unknown }) => {
        if (typeof result.owner_token !== 'string' || !result.owner_token) throw new ApiError('invalid_response', '服务未返回有效连接');
        this.#token = result.owner_token;
      }).finally(() => { this.#renewing = undefined; });
    return this.#renewing;
  }

  async restore(): Promise<boolean> {
    if (!this.#browserTicket) return false;
    try { await this.renew(); return true; }
    catch (error) {
      if (error instanceof ApiError && [401, 403].includes(error.status)) { this.clear(); return false; }
      throw error;
    }
  }

  async get<T>(path: string, signal?: AbortSignal): Promise<T> {
    return (await this.fetch(path, 'GET', undefined, undefined, signal)).json() as Promise<T>;
  }

  async command<T>(path: string, payload: unknown, key: string, method = 'POST'): Promise<T> {
    const response = await this.fetch(path, method, payload, key);
    return response.status === 204 ? undefined as T : response.json() as Promise<T>;
  }

  async download(id: string, signal?: AbortSignal): Promise<Blob> {
    return (await this.fetch(`/api/v1/artifacts/${segment(id)}?download=true`, 'GET', undefined, undefined, signal)).blob();
  }

  async downloadFile(path: string): Promise<{ blob: Blob; filename?: string }> {
    const response = await this.fetch(path);
    const disposition = response.headers.get('content-disposition') ?? '';
    const encoded = disposition.match(/filename\*=UTF-8''([^;]+)/i)?.[1];
    let name = disposition.match(/filename="([^"]+)"/i)?.[1];
    if (encoded) { try { name = decodeURIComponent(encoded); } catch { /* Keep the plain fallback. */ } }
    const filename = name?.split(/[\\/]/).at(-1)?.replace(/[\x00-\x1f\x7f]/g, '');
    return { blob: await response.blob(), filename };
  }

  async previewText(id: string, maxBytes: number, signal?: AbortSignal): Promise<string> {
    const response = await this.fetch(`/api/v1/artifacts/${segment(id)}?download=true`, 'GET', undefined, undefined, signal);
    const tooLarge = () => new ApiError('preview_too_large', '产物超过文本预览大小，请下载后查看。');
    if (Number(response.headers.get('content-length')) > maxBytes) {
      await response.body?.cancel();
      throw tooLarge();
    }
    if (!response.body) throw new ApiError('empty_artifact', '服务未提供产物内容');
    const reader = response.body.getReader(); const decoder = new TextDecoder();
    let size = 0; let text = '';
    try {
      while (true) {
        const chunk = await reader.read();
        if (chunk.done) return text + decoder.decode();
        size += chunk.value.byteLength;
        if (size > maxBytes) { await reader.cancel(); throw tooLarge(); }
        text += decoder.decode(chunk.value, { stream: true });
      }
    } finally { reader.releaseLock(); }
  }

  async stream(path: string, signal: AbortSignal): Promise<Response> {
    return this.fetch(path, 'GET', undefined, undefined, signal);
  }
}

export function segment(value: string): string {
  if (!value || value === '.' || value === '..' || /[\x00-\x1f/\\]/.test(value)) {
    throw new ApiError('invalid_id', '服务返回了无效的对象标识');
  }
  return encodeURIComponent(value);
}

export function takeBootstrap(): string | null {
  const params = new URLSearchParams(window.location.hash.slice(1));
  const code = params.get('bootstrap');
  if (params.has('bootstrap')) {
    params.delete('bootstrap');
    const remaining = params.toString();
    window.history.replaceState(null, '', window.location.pathname + window.location.search + (remaining ? `#${remaining}` : ''));
  }
  return code;
}
