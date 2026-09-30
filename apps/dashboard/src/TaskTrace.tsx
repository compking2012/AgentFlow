import { useEffect, useId, useRef, useState } from 'react';
import { ApiError, OwnerApi, segment } from './api';
import { ErrorBox, Status } from './components';
import { label } from './labels';

type Attempt = { id: string; generation: number; status: string; started_at?: string | null;
  finished_at?: string | null; duration_ms?: number | null; model?: string | null };
type AttemptsPage = { run_id: string; work_item_id: string; current_attempt_id: string | null;
  items: Attempt[]; next_before: string | null };
type TraceKind = 'instruction' | 'llm_request' | 'llm_output' | 'tool_call' | 'tool_result' | 'status' | 'error';
type TraceEvent = { id: string; seq: number; kind: TraceKind; title: string; content: string; created_at: string;
  status?: string | null; duration_ms?: number | null; model?: string | null; call_id?: string | null;
  truncated?: boolean; redacted?: boolean };
type TracePage = { attempt_id: string; run_id: string; work_item_id: string; items: TraceEvent[];
  next_after: number; next_before: number | null; has_more_after: boolean; has_more_before: boolean; complete: boolean };
type TraceWindow = TracePage & { removedFromWindow: boolean };
const kinds: Record<TraceKind, string> = { instruction: '任务指令', llm_request: '发送给模型', llm_output: '模型回复',
  tool_call: '工具操作', tool_result: '工具结果', status: '执行状态', error: '错误' };
const MAX_RESPONSE = 1024 * 1024;
const MAX_EVENT = 16 * 1024;
const MAX_WINDOW = 512 * 1024;
const encoder = new TextEncoder();
const invalid = () => new ApiError('invalid_trace', '过程记录暂时无法核对，请重新读取。');
const object = (value: unknown): value is Record<string, unknown> => Boolean(value && typeof value === 'object' && !Array.isArray(value));
const string = (value: unknown, maximum = 200): value is string => typeof value === 'string' && value.length > 0 && value.length <= maximum;
const count = (value: unknown): value is number => Number.isSafeInteger(value) && Number(value) >= 0;
const optionalText = (value: unknown, maximum = 300) => value == null || (typeof value === 'string' && value.length <= maximum);

async function readPage(api: OwnerApi, path: string, signal: AbortSignal): Promise<unknown> {
  const response = await api.stream(path, signal);
  const oversized = () => new ApiError('trace_too_large', '本次过程记录过大，请重新读取较小的范围。');
  if (Number(response.headers.get('content-length')) > MAX_RESPONSE) { await response.body?.cancel(); throw oversized(); }
  if (!response.body) throw invalid();
  const reader = response.body.getReader(); const decoder = new TextDecoder(); let bytes = 0; let text = '';
  try {
    while (true) {
      const chunk = await reader.read();
      if (chunk.done) { text += decoder.decode(); break; }
      bytes += chunk.value.byteLength;
      if (bytes > MAX_RESPONSE) { await reader.cancel(); throw oversized(); }
      text += decoder.decode(chunk.value, { stream: true });
    }
  } finally { reader.releaseLock(); }
  try { return JSON.parse(text); } catch { throw invalid(); }
}

function attemptsPage(value: unknown, runId: string, workId: string): AttemptsPage {
  if (!object(value) || value.run_id !== runId || value.work_item_id !== workId || !Array.isArray(value.items)
      || value.items.length > 20 || (value.current_attempt_id !== null && !string(value.current_attempt_id))
      || (value.next_before !== null && !string(value.next_before, 1000))) throw invalid();
  if (value.items.some(item => !object(item) || !string(item.id) || !count(item.generation)
      || !string(item.status, 60) || !optionalText(item.model) || !optionalText(item.started_at, 100) || !optionalText(item.finished_at, 100)
      || (item.duration_ms != null && (!Number.isFinite(item.duration_ms) || Number(item.duration_ms) < 0)))) throw invalid();
  return value as unknown as AttemptsPage;
}

function tracePage(value: unknown, runId: string, workId: string, attemptId: string): TracePage {
  if (!object(value) || value.run_id !== runId || value.work_item_id !== workId || value.attempt_id !== attemptId
      || !Array.isArray(value.items) || value.items.length > 50 || !count(value.next_after)
      || (value.next_before !== null && !count(value.next_before)) || typeof value.has_more_before !== 'boolean'
      || typeof value.has_more_after !== 'boolean' || typeof value.complete !== 'boolean') throw invalid();
  let prior = -1; const ids = new Set<string>();
  for (const item of value.items) {
    if (!object(item) || !string(item.id) || ids.has(item.id) || !count(item.seq) || item.seq <= prior
        || typeof item.kind !== 'string' || !Object.hasOwn(kinds, item.kind) || !string(item.title, 300)
        || typeof item.content !== 'string' || encoder.encode(item.content).byteLength > MAX_EVENT
        || !string(item.created_at, 100) || !optionalText(item.model) || !optionalText(item.status, 60) || !optionalText(item.call_id)
        || (item.redacted != null && typeof item.redacted !== 'boolean') || (item.truncated != null && typeof item.truncated !== 'boolean')
        || (item.duration_ms != null && (!Number.isFinite(item.duration_ms) || Number(item.duration_ms) < 0))) throw invalid();
    ids.add(item.id); prior = item.seq;
  }
  if (value.items.length && value.next_after < prior) throw invalid();
  return value as unknown as TracePage;
}

function mergePage(current: TraceWindow | undefined, page: TracePage, direction: 'latest' | 'before' | 'after'): TraceWindow {
  const previous = direction === 'latest' || current?.attempt_id !== page.attempt_id ? undefined : current;
  const rows = new Map((previous?.items ?? []).map(item => [item.seq, item]));
  for (const item of page.items) rows.set(item.seq, item);
  if ([...rows.keys()].some(sequence => sequence > 0)) rows.delete(0);
  const items = [...rows.values()].sort((a, b) => a.seq - b.seq);
  let bytes = items.reduce((sum, item) => sum + encoder.encode(item.content).byteLength, 0); let removed = false;
  while (items.length > 200 || bytes > MAX_WINDOW) {
    const item = direction === 'before' ? items.pop()! : items.shift()!;
    bytes -= encoder.encode(item.content).byteLength; removed = true;
  }
  return { ...page, items, next_before: items[0]?.seq ?? page.next_before,
    next_after: direction === 'before' && removed ? items.at(-1)?.seq ?? page.next_after
      : Math.max(page.next_after, previous?.next_after ?? 0, items.at(-1)?.seq ?? 0),
    has_more_before: direction === 'before' ? page.has_more_before : (previous?.has_more_before ?? page.has_more_before) || removed,
    has_more_after: direction === 'before' && previous ? previous.has_more_after || removed : page.has_more_after,
    removedFromWindow: Boolean(previous?.removedFromWindow || removed) };
}

function time(value?: string | null) {
  if (!value || !Number.isFinite(Date.parse(value))) return '时间待记录';
  return new Date(value).toLocaleString('zh-CN', { month: 'numeric', day: 'numeric', hour: '2-digit',
    minute: '2-digit', second: '2-digit', hour12: false });
}
function duration(milliseconds?: number | null) {
  if (milliseconds == null || !Number.isFinite(milliseconds) || milliseconds < 0) return '';
  if (milliseconds < 1000) return `${Math.round(milliseconds)} 毫秒`;
  const seconds = Math.round(milliseconds / 100) / 10;
  return seconds < 60 ? `${seconds} 秒` : `${Math.floor(seconds / 60)} 分 ${Math.floor(seconds % 60)} 秒`;
}

function TraceRow({ event, onInspect }: { event: TraceEvent; onInspect: () => void }) {
  const [expanded, setExpanded] = useState(false);
  return <details className={`task-trace-event trace-kind-${event.kind}`} data-trace-seq={event.seq}
    onToggle={value => { if (value.target === value.currentTarget) {
      setExpanded(value.currentTarget.open); if (value.currentTarget.open) onInspect();
    } }}>
    <summary><span className="task-trace-kind">{kinds[event.kind]}</span><strong>{event.title}</strong>
      <span className="task-trace-event-time">{time(event.created_at)}</span>
      <span className="task-trace-expand" aria-hidden="true">{expanded ? '收起' : '展开'}</span></summary>
    {expanded && <div className="task-trace-event-body"><div className="task-trace-event-meta">
      {event.status && <Status value={event.status} />}{event.model && <span>模型：{event.model}</span>}
      {event.duration_ms != null && <span>耗时 {duration(event.duration_ms)}</span>}
      {event.call_id && <span>关联操作：{event.call_id}</span>}
      {event.redacted && <span>敏感信息已隐藏</span>}</div>
      {event.content ? <pre>{event.content}</pre> : <p className="muted">此条记录没有附加正文。</p>}
      {event.truncated && <p className="hint-warning">内容较长，此处显示已保存的片段。</p>}
    </div>}
  </details>;
}

export function TaskTrace({ api, runId, workId, taskName }: { api: OwnerApi; runId: string; workId: string; taskName: string }) {
  const [open, setOpen] = useState(false);
  const [attempts, setAttempts] = useState<AttemptsPage>();
  const [attemptBefore, setAttemptBefore] = useState<string | null>(null);
  const [selected, setSelected] = useState<string>();
  const [window, setWindow] = useState<TraceWindow>();
  const [following, setFollowing] = useState(true);
  const [listBusy, setListBusy] = useState(false); const [traceBusy, setTraceBusy] = useState(false);
  const [listError, setListError] = useState<Error>(); const [traceError, setTraceError] = useState<Error>();
  const [refresh, setRefresh] = useState(0);
  const root = useRef<HTMLDetailsElement | null>(null); const viewport = useRef<HTMLDivElement | null>(null);
  const listRequest = useRef<AbortController | null>(null); const traceRequest = useRef<AbortController | null>(null);
  const currentWindow = useRef<TraceWindow | undefined>(undefined); currentWindow.current = window;
  const followingRef = useRef(following); followingRef.current = following;
  const selectedRef = useRef(selected); selectedRef.current = selected;
  const latestAttemptRef = useRef(attempts?.current_attempt_id); latestAttemptRef.current = attempts?.current_attempt_id;
  const chooseHistoricalPage = useRef(false);
  const attemptId = selected ?? attempts?.current_attempt_id ?? attempts?.items[0]?.id;
  const cachedAttempt = useRef<Attempt | undefined>(undefined);
  const listedAttempt = attempts?.items.find(item => item.id === attemptId);
  if (listedAttempt) cachedAttempt.current = listedAttempt;
  const attempt = listedAttempt ?? (cachedAttempt.current?.id === attemptId ? cachedAttempt.current : undefined);
  const scope = `${runId}:${workId}:${attemptId ?? ''}`;
  const currentScope = useRef(scope); currentScope.current = scope;
  const labelId = useId();
  const visible = () => {
    if (!root.current?.getClientRects().length || document.visibilityState === 'hidden') return false;
    for (let element: HTMLElement | null = root.current; element; element = element.parentElement) {
      if (element.hidden || (element instanceof HTMLDetailsElement && !element.open)) return false;
    }
    return true;
  };

  useEffect(() => { if (!open) { setListBusy(false); setTraceBusy(false); } }, [open]);

  useEffect(() => {
    if (!open) return;
    let active = true; let timer = 0;
    async function load() {
      if (!active) return;
      if (!visible()) { timer = globalThis.window.setTimeout(() => void load(), 2000); return; }
      const abort = new AbortController(); listRequest.current?.abort(); listRequest.current = abort;
      setListBusy(true);
      try {
        const query = attemptBefore ? `&before=${encodeURIComponent(attemptBefore)}` : '';
        const result = attemptsPage(await readPage(api, `/api/v1/runs/${segment(runId)}/work_items/${segment(workId)}/attempts?limit=20${query}`, abort.signal), runId, workId);
        if (active && !abort.signal.aborted) {
          setAttempts(result); setListError(undefined);
          if (chooseHistoricalPage.current) { chooseHistoricalPage.current = false; setSelected(result.items[0]?.id); }
        }
      } catch (error) { if (active && !abort.signal.aborted) setListError(error as Error); }
      finally { if (active && !abort.signal.aborted) { setListBusy(false); timer = globalThis.window.setTimeout(() => void load(), 2000); } }
    }
    void load();
    return () => { active = false; listRequest.current?.abort(); globalThis.window.clearTimeout(timer); };
  }, [api, runId, workId, open, attemptBefore, refresh]);

  useEffect(() => {
    setWindow(undefined); currentWindow.current = undefined; setTraceError(undefined); setFollowing(true);
    traceRequest.current?.abort(); setTraceBusy(false);
  }, [scope]);

  async function loadTrace(direction: 'latest' | 'before' | 'after', expectedScope = scope) {
    if (!attemptId || !open || !visible() || traceRequest.current) return;
    const prior = currentWindow.current;
    const cursor = direction === 'before' ? prior?.next_before : direction === 'after' ? prior?.next_after : null;
    if (direction !== 'latest' && cursor == null) return;
    const abort = new AbortController(); traceRequest.current = abort; setTraceBusy(true);
    try {
      const query = cursor == null ? '' : `&${direction}=${cursor}`;
      const result = tracePage(await readPage(api, `/api/v1/attempts/${segment(attemptId)}/trace?limit=50${query}`, abort.signal), runId, workId, attemptId);
      if (!abort.signal.aborted && currentScope.current === expectedScope) {
        const next = mergePage(currentWindow.current, result, direction);
        currentWindow.current = next; setWindow(next); setTraceError(undefined);
      }
    } catch (error) { if (!abort.signal.aborted && currentScope.current === expectedScope) setTraceError(error as Error); }
    finally {
      if (traceRequest.current === abort) { traceRequest.current = null; setTraceBusy(false); }
    }
  }

  useEffect(() => {
    if (!open || !attemptId) return;
    let active = true; let timer = 0;
    async function poll() {
      if (!active) return;
      const page = currentWindow.current;
      if (!page) await loadTrace('latest', scope);
      else if (followingRef.current && (!page.complete || page.has_more_after || selectedRef.current === undefined
          || attemptId === latestAttemptRef.current)) await loadTrace('after', scope);
      if (active) timer = globalThis.window.setTimeout(() => void poll(), 2000);
    }
    void poll();
    return () => { active = false; traceRequest.current?.abort(); traceRequest.current = null; globalThis.window.clearTimeout(timer); };
  }, [api, scope, open, refresh]);

  useEffect(() => {
    if (following && viewport.current) viewport.current.scrollTop = viewport.current.scrollHeight;
  }, [window?.items, following]);

  function latest() {
    setFollowing(true); void loadTrace('latest');
  }
  function pause() {
    setFollowing(false); if (attemptId) setSelected(attemptId);
    traceRequest.current?.abort(); traceRequest.current = null; setTraceBusy(false);
  }
  const loaded = window?.attempt_id === attemptId ? window : undefined;
  const placeholder = loaded?.items.length === 1 && loaded.items[0].seq === 0 ? loaded.items[0] : undefined;
  const elapsed = attempt?.duration_ms ?? (attempt?.started_at && Number.isFinite(Date.parse(attempt.started_at))
    ? (attempt.finished_at ? Date.parse(attempt.finished_at) : attempt.status === 'running' ? Date.now() : NaN) - Date.parse(attempt.started_at) : null);
  return <details ref={root} className="task-trace" onToggle={event => {
    if (event.target === event.currentTarget) setOpen(event.currentTarget.open);
  }}>
    <summary><span>执行过程</span><small>查看指令、模型回复和工具结果</small></summary>
    {open && <section aria-labelledby={labelId} className="task-trace-panel" aria-live="off">
      <div className="task-trace-heading"><h5 id={labelId}>{taskName}的执行过程</h5>
        <span className="task-trace-connection" role="status">{listBusy && !attempts ? '正在读取执行记录…'
          : !following ? '已暂停更新' : loaded?.complete ? '执行已结束 · 可刷新记录' : '每 2 秒更新'}</span></div>
      <div className="task-trace-attempts"><label>查看哪次执行<select value={selected ?? ''} disabled={listBusy && !attempts}
        onChange={event => setSelected(event.target.value || undefined)}>
        <option value="">最新执行（自动跟随）</option>
        {(attempts?.items ?? []).map(item => <option key={item.id} value={item.id}>第 {item.generation} 轮 · {time(item.started_at)} · {label(item.status)}</option>)}
        {selected && !attempts?.items.some(item => item.id === selected) && <option value={selected}>
          {attempt ? `第 ${attempt.generation} 轮 · ${time(attempt.started_at)}` : '已选择的历史执行'}</option>}
      </select></label>
        {attempts?.next_before && <button type="button" className="text-button" disabled={listBusy} onClick={() => {
          chooseHistoricalPage.current = true; setAttemptBefore(attempts.next_before);
        }}>更早的执行</button>}
        {attemptBefore && <button type="button" className="text-button" disabled={listBusy} onClick={() => { setSelected(undefined); setAttemptBefore(null); }}>最近的执行</button>}
      </div>
      <ErrorBox error={listError} onRetry={() => setRefresh(value => value + 1)} />
      {attempt && <div className="task-trace-attempt-meta"><Status value={attempt.status} />
        <span>开始于 {time(attempt.started_at)}</span>{duration(elapsed) && <span>耗时 {duration(elapsed)}</span>}
        {attempt.model && <span>模型：{attempt.model}</span>}</div>}
      {!attemptId && !listBusy && !listError && <p className="task-trace-empty">此任务尚未开始执行。</p>}
      {attemptId && <>
        <div className="task-trace-controls"><button type="button" className="text-button" disabled={traceBusy || !loaded?.has_more_before}
          onClick={() => { pause(); void loadTrace('before'); }}>查看更早记录</button>
          <div>{loaded?.has_more_after && <button type="button" className="text-button" disabled={traceBusy} onClick={() => void loadTrace('after')}>读取后续记录</button>}
            <button type="button" className="text-button" disabled={traceBusy} onClick={() => void loadTrace('latest')}>刷新记录</button>
            <button type="button" className="text-button" disabled={traceBusy} aria-pressed={following}
              onClick={() => following ? pause() : latest()}>{following ? '暂停更新' : '跟随最新记录'}</button></div></div>
        <ErrorBox error={traceError} onRetry={() => void loadTrace(loaded ? 'after' : 'latest')} />
        {traceBusy && !loaded && <p className="task-trace-empty" role="status">正在读取过程记录…</p>}
        {loaded && !loaded.items.length && <p className="task-trace-empty">尚无过程记录。新的指令和结果会显示在这里。</p>}
        {placeholder && <p className="task-trace-empty">{placeholder.content}</p>}
        {loaded && loaded.items.length > 0 && !placeholder && <div ref={viewport} className="task-trace-events" tabIndex={0} aria-label="过程记录列表"
          onWheel={event => { if (event.deltaY < 0) pause(); }} onTouchStart={pause}
          onKeyDown={event => { if (['ArrowUp', 'PageUp', 'Home'].includes(event.key)) pause(); }}>
          {loaded.items.map(event => <TraceRow key={`${attemptId}:${event.id}`} event={event} onInspect={pause} />)}
        </div>}
        {loaded?.removedFromWindow && <p className="task-trace-window-note">当前只展示一段记录，可用“更早”或“后续”分段查看。</p>}
      </>}
    </section>}
  </details>;
}
