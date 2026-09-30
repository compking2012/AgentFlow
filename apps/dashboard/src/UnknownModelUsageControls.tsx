import { useEffect, useRef, useState } from 'react';
import type { FormEvent } from 'react';
import { ApiError, OwnerApi, segment } from './api';
import { ErrorBox, Field } from './components';
import { useCommand } from './hooks';
import type { ModelUncertainties, ModelUncertainty, Run } from './types';

type Confirmation = { expected_run_revision: number; expected_work_revision: number; expected_attempt_revision: number;
  expected_invocation_revision: number; expected_attempt_budget_revision: number; expected_state_digest: string;
  accept_unknown_usage: true; reason: string };
function valid(value: ModelUncertainty | undefined, runId: string, invocationId: string) {
  return Boolean(value && value.run_id === runId && value.invocation_id === invocationId
    && typeof value.work_item_id === 'string' && typeof value.attempt_id === 'string'
    && [value.run_revision, value.work_revision, value.attempt_revision, value.invocation_revision, value.attempt_budget_revision]
      .every(revision => Number.isInteger(revision) && revision >= 1)
    && /^sha256:[a-f0-9]{64}$/.test(value.expected_state_digest) && Array.isArray(value.blockers)
    && value.blockers.every(blocker => blocker && typeof blocker.code === 'string' && typeof blocker.message === 'string')
    && typeof value.acknowledged === 'boolean' && typeof value.eligible === 'boolean'
    && value.requires_separate_retry === true);
}
function eligible(value: ModelUncertainty) {
  return value.eligible === true && value.cost_mode === 'request_limited' && value.state === 'uncertain'
    && value.request_counted === true && !value.acknowledged;
}

export function UnknownModelUsageControls({ api, run, item, onChanged, onVisibilityChange }: {
  api: OwnerApi; run: Run; item: ModelUncertainty; onChanged: () => void;
  onVisibilityChange?: (item: ModelUncertainty, visible: boolean) => void;
}) {
  const command = useCommand(api);
  const [snapshot, setSnapshot] = useState<ModelUncertainty>();
  const [confirmed, setConfirmed] = useState<{ source: ModelUncertainty; receipt: ModelUncertainty }>();
  const [open, setOpen] = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<Error>();
  const [accepted, setAccepted] = useState(false);
  const [reason, setReason] = useState('');
  const [sent, setSent] = useState<Confirmation>();
  const sentRef = useRef<Confirmation | undefined>(undefined);
  const busy = useRef(false);
  const alive = useRef(true);
  useEffect(() => { alive.current = true; return () => { alive.current = false; }; }, []);
  // Only bridge the gap until the next server preview. New evidence wins in
  // the same render, including when it arrives before a delayed receipt.
  const current = confirmed?.source === item ? confirmed.receipt : item;
  const correct = valid(current, run.id, item.invocation_id);
  const visible = !correct || !current.acknowledged || Boolean(sent);
  useEffect(() => { onVisibilityChange?.(item, visible); }, [item, visible, onVisibilityChange]);
  const canConfirm = correct && eligible(current);
  const stale = Boolean(snapshot && (snapshot.run_revision !== run.revision
    || snapshot.expected_state_digest !== item.expected_state_digest));
  async function inspect() {
    if (busy.current || sentRef.current) return;
    busy.current = true; setLoading(true); setError(undefined);
    try {
      const result = await api.get<ModelUncertainties>(`/api/v1/runs/${segment(run.id)}/model_uncertainties`);
      const selected = result?.items?.find(value => value.invocation_id === item.invocation_id);
      if (result?.run_id !== run.id || !valid(selected, run.id, item.invocation_id)) {
        throw new ApiError('invalid_response', '模型调用记录无法核对，请重新读取恢复状态。');
      }
      if (!alive.current) return;
      setSnapshot(selected); setAccepted(false); setReason(''); setOpen(true); command.clearError();
      onChanged();
    } catch (cause) { if (alive.current) setError(cause as Error); }
    finally { busy.current = false; if (alive.current) setLoading(false); }
  }
  async function acknowledge(event: FormEvent) {
    event.preventDefault();
    if (busy.current || (!sentRef.current && (!snapshot || !eligible(snapshot) || !accepted || !reason.trim() || stale))) return;
    const payload = sentRef.current ?? { expected_run_revision: snapshot!.run_revision, expected_work_revision: snapshot!.work_revision,
      expected_attempt_revision: snapshot!.attempt_revision, expected_invocation_revision: snapshot!.invocation_revision,
      expected_attempt_budget_revision: snapshot!.attempt_budget_revision, expected_state_digest: snapshot!.expected_state_digest,
      accept_unknown_usage: true, reason: reason.trim() };
    sentRef.current = payload; setSent(payload); busy.current = true;
    try {
      const result = await command.execute<ModelUncertainty>(`/api/v1/runs/${segment(run.id)}/model_invocations/${segment(item.invocation_id)}/acknowledge_unknown`, payload, 'POST', value => {
        if (!valid(value, run.id, item.invocation_id) || value.work_item_id !== snapshot!.work_item_id
          || value.attempt_id !== snapshot!.attempt_id || value.state !== 'uncertain' || value.cost_mode !== 'request_limited'
          || !value.acknowledged || typeof value.acknowledgment_id !== 'string' || !value.acknowledgment_id
          || value.request_counted !== true || value.actual_micros !== snapshot!.actual_micros) {
          throw new ApiError('invalid_response', '确认回执无法核对，请核对原确认操作。');
        }
      });
      if (!result || !alive.current) return;
      setConfirmed({ source: item, receipt: result }); setOpen(false); sentRef.current = undefined; setSent(undefined); onChanged();
    } catch (cause) {
      if (!alive.current) return;
      if (cause instanceof ApiError && [409, 422].includes(cause.status)) {
        sentRef.current = undefined; setSent(undefined); setSnapshot(undefined);
      }
      onChanged();
    } finally { busy.current = false; }
  }
  if (!visible) return null;
  const title = typeof current.work_title === 'string' ? Array.from(current.work_title) : [];
  return <section className="model-uncertainty" aria-label="模型调用未知用量确认">
    <h3 title={current.work_title}>{title.length ? title.slice(0, 100).join('') + (title.length > 100 ? '…' : '') : '调用确认'}</h3>
    <details><summary>关联调用记录</summary><p>调用：<code>{item.invocation_id}</code></p><p>执行：<code>{item.attempt_id}</code></p></details>
    {!correct && <p className="hint-warning">调用身份或版本不完整，暂不能确认。</p>}
    {correct && current.blockers.length > 0 && <ul>{current.blockers.map(blocker => <li key={blocker.code + blocker.message}>{blocker.message}</li>)}</ul>}
    {current.cost_mode !== 'request_limited' && <p className="hint-warning">仅按请求次数管理的运行支持此确认；金额预算模式仍需核对真实用量。</p>}
    <ErrorBox error={error} />
    {!open && !current.acknowledged && <div className="form-actions"><button className="button secondary small" disabled={loading || !canConfirm}
      onClick={() => void inspect()}>{loading ? '正在核验调用…' : '查看并确认未知用量'}</button>
      <button className="button secondary small" onClick={onChanged}>重新读取恢复状态</button></div>}
    {open && <form className="recovery-limit" onSubmit={acknowledge}>
      <label className="inline-check"><input type="checkbox" checked={accepted} disabled={Boolean(sent) || !snapshot}
        onChange={event => setAccepted(event.target.checked)} />我理解该调用可能已计费，并同意保留未知用量、未知费用和原调用次数</label>
      <Field label="确认说明"><textarea rows={2} maxLength={2000} required value={reason} disabled={Boolean(sent) || !snapshot}
        onChange={event => setReason(event.target.value)} /></Field>
      {!sent && (stale || !snapshot) && <p className="hint-warning">调用状态已变化，请返回重新核验。</p>}
      {snapshot && !eligible(snapshot) && !sent && <p className="hint-warning">当前调用不满足确认条件，请查看恢复阻塞说明。</p>}
      {sent && command.error && <p className="muted">结果尚未确认；核对原确认会复用相同调用、版本和提交标识。</p>}
      <ErrorBox error={command.error} />
      <div className="form-actions"><button className="button small" disabled={command.busy || (!sent
        && (!snapshot || !eligible(snapshot) || !accepted || !reason.trim() || stale))}>
        {command.busy ? '正在确认…' : sent ? '核对原未知用量确认' : '确认保留未知用量并继续核验'}</button>
        <button type="button" className="button secondary small" disabled={command.busy || Boolean(sent)} onClick={() => setOpen(false)}>返回</button></div>
    </form>}
  </section>;
}
