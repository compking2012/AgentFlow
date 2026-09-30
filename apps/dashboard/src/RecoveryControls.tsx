import { useCallback, useEffect, useRef, useState } from 'react';
import type { FormEvent } from 'react';
import { ApiError, OwnerApi, segment } from './api';
import { ErrorBox, Field } from './components';
import { useCommand, useDialogFocus } from './hooks';
import type { Run, WorkExecutionBudget, ModelUncertainties, ModelUncertainty } from './types';
import { WorkExecutionBudgetControls } from './WorkExecutionBudgetControls';
import { UnknownModelUsageControls } from './UnknownModelUsageControls';

export type RecoveryBlocker = { code: string; message: string };
export type RetryOption = { work_item_id: string; stage_key: string; step: string; status: string;
  eligible: boolean; blockers: RecoveryBlocker[]; affected_work_item_ids: string[]; preserved_work_item_ids: string[];
  checkpoint?: { kind: string; commit_oid?: string }; execution_budget?: WorkExecutionBudget };
export type RecoveryOptions = { run_id: string; expected_revision: number;
  continue: { eligible: boolean; blockers: RecoveryBlocker[]; affected_work_item_ids?: string[]; preserved_work_item_ids?: string[] }; retry_options: RetryOption[];
  model_uncertainties?: ModelUncertainties };
export type RecoveryIntent = { mode: 'retry' | 'continue'; option?: RetryOption };
type RecoveryReceipt = { id: string; run_id: string; mode: string; run: Run };

export function RecoveryControls({ api, run, options, intent, onIntent, onChanged }: {
  api: OwnerApi; run: Run; options?: RecoveryOptions; intent: RecoveryIntent | null;
  onIntent: (intent: RecoveryIntent | null) => void; onChanged: () => void;
}) {
  const command = useCommand(api); const limitCommand = useCommand(api);
  const [reason, setReason] = useState(''); const [revision, setRevision] = useState(0);
  const [useCurrentModelSettings, setUseCurrentModelSettings] = useState(false);
  const [sent, setSent] = useState<Record<string, unknown>>();
  const sentRef = useRef<Record<string, unknown> | undefined>(undefined);
  const [notice, setNotice] = useState(''); const [limitOpen, setLimitOpen] = useState(false);
  const [limit, setLimit] = useState(''); const [limitRevision, setLimitRevision] = useState(0);
  const [limitSent, setLimitSent] = useState<Record<string, unknown>>();
  const limitSentRef = useRef<Record<string, unknown> | undefined>(undefined);
  const budgetOptionsRef = useRef<{ runId: string; items: RetryOption[] }>({ runId: run.id, items: [] });
  if (budgetOptionsRef.current.runId !== run.id) budgetOptionsRef.current = { runId: run.id, items: [] };
  if (options?.run_id === run.id) budgetOptionsRef.current.items = options.retry_options.filter(option => option.execution_budget);
  const budgetOptions = budgetOptionsRef.current.items;
  const uncertaintyRef = useRef<{ runId: string; items: ModelUncertainty[] }>({ runId: run.id, items: [] });
  if (uncertaintyRef.current.runId !== run.id) uncertaintyRef.current = { runId: run.id, items: [] };
  if (options?.model_uncertainties?.run_id === run.id && Array.isArray(options.model_uncertainties.items)) uncertaintyRef.current.items = options.model_uncertainties.items;
  const uncertainties = uncertaintyRef.current.items;
  const [uncertaintyVisibility, setUncertaintyVisibility] = useState<Record<string, { item: ModelUncertainty; visible: boolean }>>({});
  const updateUncertaintyVisibility = useCallback((item: ModelUncertainty, visible: boolean) => {
    const key = `${item.run_id}:${item.invocation_id}`;
    setUncertaintyVisibility(previous => previous[key]?.item === item && previous[key].visible === visible
      ? previous : { ...previous, [key]: { item, visible } });
  }, []);
  const uncertaintyCount = uncertainties.filter(item => {
    const state = uncertaintyVisibility[`${run.id}:${item.invocation_id}`];
    return state?.item === item ? state.visible : !item.acknowledged;
  }).length;
  useDialogFocus(Boolean(intent), () => onIntent(null), command.busy);
  useEffect(() => {
    if (intent) { setReason(''); setUseCurrentModelSettings(false); setRevision(options?.expected_revision ?? run.revision); setSent(undefined); sentRef.current = undefined; command.clearError(); }
  }, [intent]);
  useEffect(() => { onIntent(null); setLimitOpen(false); setNotice(''); setUncertaintyVisibility({}); }, [run.id]);
  const retry = options?.retry_options[0];
  const blockers = [...new Map([...(options?.retry_options.flatMap(option => option.blockers) ?? []), ...(options?.continue.blockers ?? [])]
    .filter(blocker => !['run_not_paused', 'retry_required', 'no_pending_work'].includes(blocker.code))
    .map(blocker => [blocker.code + blocker.message, blocker])).values()];
  const budgetBlocked = blockers.some(blocker => blocker.code === 'recovery_budget_exhausted');
  const selected = intent?.option;
  const displayed = selected ?? (intent?.mode === 'retry' ? retry : undefined);
  const canUseCurrentModelSettings = intent?.mode === 'retry' && ['implementation', 'unit_test_implementation',
    'integration_test_implementation'].includes(displayed?.step ?? '');
  const allowed = intent?.mode === 'continue' ? options?.continue.eligible
    : (selected ? options?.retry_options.find(option => option.work_item_id === selected.work_item_id) : retry)?.eligible;
  const canAdjustLimit = ['paused', 'cancelled', 'completed'].includes(run.execution_state);
  const currentMaximum = run.budget_limit?.max_model_requests;
  const maximum = Number(limit);
  const validLimit = /^\d+$/.test(limit) && Number.isInteger(maximum) && maximum <= 2000
    && (maximum === 0 || (typeof currentMaximum === 'number' && currentMaximum > 0 && maximum > currentMaximum));
  async function submit(event: FormEvent) {
    event.preventDefault(); if (!intent) return;
    const payload = sentRef.current ?? { expected_revision: revision, mode: intent.mode,
      ...(selected ? { work_item_id: selected.work_item_id } : {}),
      ...(intent.mode === 'retry' ? { use_current_model_settings: canUseCurrentModelSettings && useCurrentModelSettings } : {}), reason: reason.trim() };
    sentRef.current = payload; setSent(payload);
    try {
      const receipt = await command.execute<RecoveryReceipt>(`/api/v1/runs/${segment(run.id)}/recover`, payload, 'POST', value => {
        if (!value || typeof value.id !== 'string' || value.run_id !== run.id || value.run?.id !== run.id || value.mode !== payload.mode)
          throw new ApiError('invalid_response', '恢复回执无法核对，请重试核对原操作。');
      });
      if (!receipt) return;
      setNotice(intent.mode === 'retry' ? '已从保存的进度重新运行，后续步骤将按门禁继续。' : '已继续运行，已有成果保留。');
      onIntent(null); onChanged();
    } catch { onChanged(); }
  }
  async function changeLimit(event: FormEvent) {
    event.preventDefault();
    const payload = limitSentRef.current ?? { expected_revision: limitRevision, max_model_requests: maximum,
      reason: maximum === 0 ? '所有者将本轮模型调用设置为不限次数' : `所有者将本轮调用上限追加至 ${maximum} 次` };
    limitSentRef.current = payload; setLimitSent(payload);
    try {
      const receipt = await limitCommand.execute<Run>(`/api/v1/runs/${segment(run.id)}/request_limit`, payload, 'POST', value => {
        if (!value || value.id !== run.id || value.budget_limit?.max_model_requests !== payload.max_model_requests)
          throw new ApiError('invalid_response', '额度回执无法核对，请重试核对原操作。');
      });
      if (!receipt) return;
      setNotice('本轮调用上限已更新；请选择需要恢复的步骤。'); setLimitOpen(false); onChanged();
    } catch { onChanged(); }
  }
  return <div className="recovery-controls" hidden={!retry && !budgetOptions.length && !uncertaintyCount && !['paused', 'cancelled'].includes(run.execution_state) && !intent && !notice && !limitOpen}>
    <div className="form-actions">
      {retry && <button className="button secondary small" disabled={!retry.eligible || !options}
        onClick={() => onIntent({ mode: 'retry' })}>重试中断步骤</button>}
      {['paused', 'cancelled'].includes(run.execution_state) && <button className="button secondary small" disabled={!options?.continue.eligible}
        onClick={() => onIntent({ mode: 'continue' })}>继续运行</button>}
      {budgetBlocked && <button className="button secondary small" disabled={!canAdjustLimit}
        onClick={() => { setLimit(String(currentMaximum ?? 200)); setLimitRevision(run.revision); setLimitSent(undefined); limitSentRef.current = undefined; limitCommand.clearError(); setLimitOpen(true); }}>调整本轮调用上限</button>}
    </div>
    {blockers.length > 0 && <div className="recovery-blockers"><strong>恢复前需要处理</strong><ul>{blockers.map(blocker => <li key={blocker.code + blocker.message}>{blocker.message}</li>)}</ul>
      {budgetBlocked && !canAdjustLimit && <p>先暂停本轮，待执行结束后可调整本轮调用上限。</p>}</div>}
    <details key={run.id} className="recovery-conditions" hidden={!uncertaintyCount}>
      <summary>处理恢复条件（{uncertaintyCount}）</summary>
      <p className="muted">调用可能已计费。确认会保留未知用量、费用和原调用次数，不会记为零费用或已结算，也不会启动任务。</p>
      {uncertainties.map(item => <UnknownModelUsageControls key={`${run.id}:${item.invocation_id}`}
        api={api} run={run} item={item} onChanged={onChanged} onVisibilityChange={updateUncertaintyVisibility} />)}
    </details>
    {budgetOptions.map(option => <WorkExecutionBudgetControls
      key={`${run.id}:${option.work_item_id}`} api={api} run={run} workId={option.work_item_id} step={option.step}
      summary={option.execution_budget!} canRetry={options?.retry_options.find(item => item.work_item_id === option.work_item_id)?.eligible === true
        && options.expected_revision === run.revision}
      onRetry={() => onIntent({ mode: 'retry', option })} onChanged={onChanged} />)}
    {notice && <p role="status" className="notice">{notice}</p>}
    {limitOpen && <form className="recovery-limit" onSubmit={changeLimit}>
      <Field label="本轮模型调用上限" hint="0 表示不限次数；已用次数和费用记录保留。该操作不会自动启动模型。"><input type="number" min="0" max="2000" step="1" required value={limit}
        disabled={Boolean(limitSent)} onChange={event => setLimit(event.target.value)} /></Field>
      <ErrorBox error={limitCommand.error} /><div className="form-actions"><button className="button small" disabled={limitCommand.busy || (!limitSent && (!validLimit || limitRevision !== run.revision))}>{limitCommand.busy ? '正在提交…' : limitSent ? '核对原提交' : '更新本轮上限'}</button>
        <button type="button" className="button secondary small" disabled={limitCommand.busy} onClick={() => setLimitOpen(false)}>返回</button></div>
    </form>}
    {intent && <div className="modal-backdrop"><section className="modal" role="dialog" aria-modal="true" aria-labelledby="recovery-title">
      <h2 id="recovery-title">{intent.mode === 'retry' ? '重新运行此步骤' : '继续本轮运行'}</h2>
      <p>已完成的上游和无关并行任务保留；受影响的后续步骤会重新验证。</p>
      {displayed && <p className="muted">从{displayed.checkpoint?.kind === 'upstream_checkpoint' ? '有效上游成果' : '保存的代码进度'}开始，重新处理 {displayed.affected_work_item_ids.length} 项工作，保留 {displayed.preserved_work_item_ids.length} 项工作。</p>}
      {intent.mode === 'continue' && run.execution_state === 'cancelled' && options?.continue.affected_work_item_ids && <p className="muted">
        恢复本轮未完成的步骤，重新处理 {options.continue.affected_work_item_ids.length} 项工作，保留 {options.continue.preserved_work_item_ids?.length ?? 0} 项工作。后续步骤仍按依赖和审核要求运行。</p>}
      <form onSubmit={submit}><Field label="补充说明（可选）"><textarea rows={3} maxLength={2000} value={reason} disabled={Boolean(sent)} onChange={event => setReason(event.target.value)} /></Field>
        {canUseCurrentModelSettings && <div className="notice"><label className="inline-actions">
          <input type="checkbox" checked={useCurrentModelSettings} disabled={command.busy || Boolean(sent)}
            aria-describedby="recovery-model-settings-hint" onChange={event => setUseCurrentModelSettings(event.target.checked)} />
          <span>使用当前模型设置重试</span></label>
          <p id="recovery-model-settings-hint" className="muted">更换模型或推理设置后选择；同一模型的单次输出额度自动读取配置文件。不增加本轮调用次数。</p>
        </div>}
        {!sent && revision !== run.revision && <p className="hint-warning">状态已变化，请返回并重新选择。</p>}
        {sent && command.error && <p className="muted">再次提交会核对同一操作，不会重复启动。</p>}
        <ErrorBox error={command.error} /><div className="form-actions"><button className="button" disabled={command.busy || (!sent && (!allowed || revision !== run.revision))}>{command.busy ? '正在提交…' : sent ? '核对并重试提交' : '确认并运行'}</button>
          <button type="button" className="button secondary" disabled={command.busy} onClick={() => onIntent(null)}>返回</button></div>
      </form>
    </section></div>}
  </div>;
}
