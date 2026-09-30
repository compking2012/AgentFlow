import { useCallback, useEffect, useRef, useState } from 'react';
import { ApiError, OwnerApi, segment } from './api';

export type ResourceState<T> = { data?: T; error?: Error; loading: boolean; updatedAt?: Date; refresh: () => void };

export function useResource<T>(api: OwnerApi, path: string | null, collection = false, intervalMs = 8000): ResourceState<T> {
  const [nonce, setNonce] = useState(0);
  const [state, setState] = useState<Omit<ResourceState<T>, 'refresh'> & { path: string | null }>({ path, loading: Boolean(path) });
  const refresh = useCallback(() => setNonce(n => n + 1), []);
  useEffect(() => {
    if (!path) { setState({ path, loading: false }); return; }
    let active = true; let busy = false;
    const abort = new AbortController();
    setState(previous => previous.path === path ? { ...previous, loading: true, error: undefined } : { path, loading: true });
    async function load() {
      if (busy) return;
      busy = true;
      try {
        const response = await api.get<unknown>(path!, abort.signal);
        if (!response || typeof response !== 'object') throw new ApiError('invalid_response', '服务返回了无效数据');
        const data = collection ? (response as { items?: unknown }).items : response;
        if (collection && !Array.isArray(data)) throw new ApiError('invalid_response', '服务未返回有效列表');
        if (active) setState({ path, data: data as T, loading: false, updatedAt: new Date() });
      } catch (error) {
        if (active && !abort.signal.aborted) setState({ path, error: error as Error, loading: false });
      } finally { busy = false; }
    }
    void load();
    const timer = window.setInterval(() => void load(), intervalMs);
    return () => { active = false; abort.abort(); window.clearInterval(timer); };
  }, [api, path, collection, nonce, intervalMs]);
  return state.path === path ? { ...state, refresh } : { loading: Boolean(path), refresh };
}

/** Reuse one idempotency key for retries of the same unresolved form submission. */
export function useCommand(api: OwnerApi) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<Error>();
  const inFlight = useRef(false);
  const intent = useRef<{ signature: string; key: string } | null>(null);
  async function execute<T>(path: string, payload: unknown, method = 'POST', validate?: (value: T) => void): Promise<T | undefined> {
    if (inFlight.current) return;
    inFlight.current = true; setBusy(true); setError(undefined);
    const signature = `${method}:${path}:${JSON.stringify(payload)}`;
    if (intent.current?.signature !== signature) intent.current = { signature, key: crypto.randomUUID() };
    try {
      const result = await api.command<T>(path, payload, intent.current.key, method);
      validate?.(result);
      intent.current = null;
      return result;
    } catch (cause) { setError(cause as Error); throw cause; }
    finally { inFlight.current = false; setBusy(false); }
  }
  return { execute, busy, error, clearError: () => setError(undefined) };
}

export function useRunEvents(api: OwnerApi, runId: string | null, onChange: () => void) {
  const [status, setStatus] = useState('未选择运行');
  const callback = useRef(onChange);
  callback.current = onChange;
  useEffect(() => {
    if (!runId) { setStatus('未选择运行'); return; }
    const abort = new AbortController();
    let cursor = 0; let timer = 0; let retry = 1000;
    async function connect() {
      try {
        setStatus('连接事件流');
        const response = await api.stream(`/api/v1/runs/${segment(runId!)}/events?after=${cursor}`, abort.signal);
        if (!response.body) throw new Error('事件流不可用');
        setStatus('实时连接'); retry = 1000;
        const reader = response.body.getReader(); const decoder = new TextDecoder(); let buffer = '';
        try {
          while (!abort.signal.aborted) {
            const { done, value } = await reader.read();
            if (done) break;
            buffer += decoder.decode(value, { stream: true }).replace(/\r\n/g, '\n');
            if (buffer.length > 1024 * 1024) throw new Error('事件长度超过限制');
            let index: number;
            while ((index = buffer.indexOf('\n\n')) >= 0) {
              const block = buffer.slice(0, index); buffer = buffer.slice(index + 2);
              const id = block.match(/^id: (\d+)$/m);
              if (id && Number(id[1]) > cursor) { cursor = Number(id[1]); callback.current(); }
            }
          }
        } finally { reader.releaseLock(); }
        if (abort.signal.aborted) return;
        setStatus('事件流已断开，正在重连');
      } catch {
        if (abort.signal.aborted) return;
        setStatus('事件流未连接，定时刷新仍可用');
      }
      timer = window.setTimeout(() => void connect(), retry); retry = Math.min(retry * 2, 10000);
    }
    void connect();
    return () => { abort.abort(); window.clearTimeout(timer); };
  }, [api, runId]);
  return status;
}

export function useDialogFocus(open: boolean, close: () => void, busy: boolean) {
  const current = useRef({ close, busy }); current.current = { close, busy };
  useEffect(() => {
    if (!open) return;
    const previous = document.activeElement as HTMLElement | null;
    const dialog = [...document.querySelectorAll<HTMLElement>('[role="dialog"]')].find(element => element.getClientRects().length > 0);
    if (!dialog) return;
    const available = () => [...dialog.querySelectorAll<HTMLElement>('button:not(:disabled), input:not(:disabled), textarea:not(:disabled), select:not(:disabled), [tabindex="0"]')].filter(e => e.getClientRects().length > 0);
    (dialog.querySelector<HTMLElement>('textarea') ?? available()[0])?.focus({ preventScroll: true });
    function key(event: KeyboardEvent) {
      if (event.key === 'Escape' && !current.current.busy) { event.preventDefault(); current.current.close(); }
      if (event.key === 'Tab') {
        const elements = available(); const first = elements[0]; const last = elements.at(-1);
        if (!first || !last) { event.preventDefault(); return; }
        if (event.shiftKey && (document.activeElement === first || !dialog!.contains(document.activeElement))) { event.preventDefault(); last.focus(); }
        else if (!event.shiftKey && (document.activeElement === last || !dialog!.contains(document.activeElement))) { event.preventDefault(); first.focus(); }
      }
    }
    document.addEventListener('keydown', key);
    return () => { document.removeEventListener('keydown', key); if (previous?.isConnected) previous.focus({ preventScroll: true }); };
  }, [open]);
}
