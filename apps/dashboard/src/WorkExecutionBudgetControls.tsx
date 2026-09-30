import { useEffect, useRef, useState } from 'react';
import type { FormEvent } from 'react';
import { ApiError, OwnerApi, segment } from './api';
import { ErrorBox, Field } from './components';
import { useCommand } from './hooks';
import { stepNames } from './labels';
import type { ExecutionBudgetKey, Run, WorkExecutionBudget } from './types';

const dimensions: { key: ExecutionBudgetKey; field: 'additional_tool_calls' | 'additional_active_seconds' | 'additional_steps'; label: string; step: string }[] = [
  { key: 'max_tool_calls', field: 'additional_tool_calls', label: '追加工具调用次数', step: '1' },
  { key: 'max_active_seconds', field: 'additional_active_seconds', label: '追加执行时长（秒）', step: '0.1' },
  { key: 'max_steps', field: 'additional_steps', label: '追加小步次数', step: '1' },
];
const emptyValues = { additional_tool_calls: '0', additional_active_seconds: '0', additional_steps: '0' };
type Extension = { expected_run_revision: number; expected_work_revision: number; expected_budget_revision: number;
  additional_tool_calls: number; additional_active_seconds: number; additional_steps: number; reason: string };
const finite = (value: unknown): value is number => typeof value === 'number' && Number.isFinite(value);
const number = (value: unknown) => finite(value) ? (value > 0 && value < 0.1 ? '< 0.1'
  : value.toLocaleString('zh-CN', { maximumFractionDigits: 1 })) : '待核验';

function verifiedShape(value: WorkExecutionBudget | undefined, runId: string, workId: string) {
  return Boolean(value && value.run_id === runId && value.work_item_id === workId
    && Number.isInteger(value.run_revision) && Number.isInteger(value.work_revision)
    && ['known', 'unknown', 'missing'].includes(value.metering) && typeof value.can_extend === 'boolean'
    && Array.isArray(value.dimensions) && value.dimensions.every(row => row && typeof row.label === 'string'
      && typeof row.unit === 'string' && dimensions.some(item => item.key === row.key))
    && Array.isArray(value.adjustment_blockers) && value.adjustment_blockers.every(row => row
      && typeof row.code === 'string' && typeof row.message === 'string'));
}
function knownBudget(value: WorkExecutionBudget | undefined) {
  return Boolean(value && value.metering === 'known' && Number.isInteger(value.budget_revision)
    && value.dimensions.length === dimensions.length && dimensions.every(({ key }) => {
      const rows = value.dimensions.filter(row => row.key === key);
      return rows.length === 1 && finite(rows[0].used) && finite(rows[0].limit)
        && finite(rows[0].remaining) && finite(rows[0].balance) && finite(rows[0].overrun)
        && typeof rows[0].exhausted === 'boolean' && rows[0].used! >= 0 && rows[0].limit! >= 0
        && rows[0].remaining! >= 0 && rows[0].overrun! >= 0;
    }));
}
function compareVersion(left: WorkExecutionBudget, right: WorkExecutionBudget) {
  for (const field of ['run_revision', 'work_revision', 'budget_revision'] as const) {
    const difference = (left[field] ?? -1) - (right[field] ?? -1);
    if (difference) return difference;
  }
  return 0;
}

export function WorkExecutionBudgetControls({ api, run, workId, step, summary, canRetry, onRetry, onChanged }: {
  api: OwnerApi; run: Run; workId: string; step: string; summary: WorkExecutionBudget; canRetry: boolean;
  onRetry: () => void; onChanged: () => void;
}) {
  const command = useCommand(api);
  const [open, setOpen] = useState(false);
  const [snapshot, setSnapshot] = useState<WorkExecutionBudget>();
  const [detail, setDetail] = useState<WorkExecutionBudget>();
  const [loading, setLoading] = useState(false);
  const [readError, setReadError] = useState<Error>();
  const [values, setValues] = useState(emptyValues);
  const [reason, setReason] = useState('');
  const [sent, setSent] = useState<Extension>();
  const sentRef = useRef<Extension | undefined>(undefined);
  const [notice, setNotice] = useState('');
  const [savedRevision, setSavedRevision] = useState<number>();
  const alive = useRef(true);
  const reading = useRef(false);
  useEffect(() => { alive.current = true; return () => { alive.current = false; }; }, []);
  const path = `/api/v1/runs/${segment(run.id)}/work_items/${segment(workId)}/execution_budget`;
  const summaryValid = verifiedShape(summary, run.id, workId);
  // A new GET can discover uncertain usage without changing budget revision.
  // Prefer that response until a newer options response arrives, even at the
  // same version. Older in-flight options cannot replace a newer budget.
  useEffect(() => {
    if (summaryValid) setDetail(previous => previous && compareVersion(previous, summary) > 0 ? previous : undefined);
  }, [summary]);
  const current = detail && (!summaryValid || detail.metering !== 'known' || compareVersion(detail, summary) >= 0) ? detail : summary;
  const valid = verifiedShape(current, run.id, workId);
  const workTitle = typeof current?.work_title === 'string' ? Array.from(current.work_title) : [];
  const known = valid && knownBudget(current);
  const stale = Boolean(snapshot && (snapshot.run_revision !== run.revision
    || snapshot.work_revision !== current.work_revision || snapshot.budget_revision !== current.budget_revision));
  const amounts = Object.fromEntries(dimensions.map(({ field }) => [field, Number(values[field])])) as
    Pick<Extension, 'additional_tool_calls' | 'additional_active_seconds' | 'additional_steps'>;
  const maximumAddition = (key: ExecutionBudgetKey) => Math.max(0, (key === 'max_active_seconds' ? 86400 : Number.MAX_SAFE_INTEGER)
    - (snapshot?.dimensions.find(row => row.key === key)?.limit ?? 0));
  const validAmounts = dimensions.every(({ key, field, step: increment }) => /^\d+(?:\.\d+)?$/.test(values[field])
    && finite(amounts[field]) && amounts[field] >= 0
    && amounts[field] <= maximumAddition(key)
    && (increment === '1' ? Number.isSafeInteger(amounts[field]) : amounts[field] <= Number.MAX_SAFE_INTEGER))
    && dimensions.some(({ field }) => amounts[field] > 0);
  const enough = snapshot && knownBudget(snapshot) && dimensions.every(({ key, field }) => {
    const row = snapshot.dimensions.find(item => item.key === key)!;
    return row.limit! + amounts[field] > row.used!;
  });
  const freshRecovery = savedRevision !== undefined && summaryValid && summary.budget_revision !== null
    && summary.budget_revision >= savedRevision && summary.run_revision === run.revision;

  async function readBudget(edit: boolean) {
    if (reading.current || command.busy || sentRef.current) return;
    reading.current = true; setLoading(true); setReadError(undefined);
    try {
      const value = await api.get<WorkExecutionBudget>(path);
      if (!verifiedShape(value, run.id, workId)) throw new ApiError('invalid_response', '工作执行额度记录无法核对，请重新读取。');
      if (!alive.current) return;
      setDetail(value);
      if (edit) { setSnapshot(value); setValues(emptyValues); setReason(''); setNotice(''); command.clearError(); setOpen(true); }
      onChanged();
    } catch (error) { if (alive.current) setReadError(error as Error); }
    finally { reading.current = false; if (alive.current) setLoading(false); }
  }

  async function extend(event: FormEvent) {
    event.preventDefault();
    if (!sentRef.current && (!snapshot || !known || !snapshot.can_extend || stale || !validAmounts || !enough || !reason.trim())) return;
    const payload = sentRef.current ?? { expected_run_revision: snapshot!.run_revision,
      expected_work_revision: snapshot!.work_revision, expected_budget_revision: snapshot!.budget_revision!,
      ...amounts, reason: reason.trim() };
    sentRef.current = payload; setSent(payload);
    try {
      const receipt = await command.execute<WorkExecutionBudget>(`${path}/extend`, payload, 'POST', value => {
        if (!verifiedShape(value, run.id, workId) || !knownBudget(value)
          || value.budget_revision! <= payload.expected_budget_revision
          || dimensions.some(({ key, field }) => value.dimensions.find(row => row.key === key)?.limit
            !== snapshot!.dimensions.find(row => row.key === key)!.limit! + payload[field])) {
          throw new ApiError('invalid_response', '追加额度回执无法核对，请核对原提交。');
        }
      });
      if (!receipt || !alive.current) return;
      setDetail(receipt); setSavedRevision(receipt.budget_revision!); setOpen(false);
      sentRef.current = undefined; setSent(undefined);
      setNotice('工作执行额度已追加，历史用量保留。正在重新核对恢复条件，尚未启动任务。');
      onChanged();
    } catch (error) {
      if (!alive.current) return;
      if (error instanceof ApiError && error.status === 409) {
        // A definite conflict must be reviewed from a fresh snapshot; an
        // uncertain transport result keeps the original operation unchanged.
        sentRef.current = undefined; setSent(undefined); setSnapshot(undefined);
      } else if (error instanceof ApiError && error.status === 422) {
        // Validation rejection is confirmed before mutation, so the owner can
        // correct the form. Do not apply this to an ambiguous network failure.
        sentRef.current = undefined; setSent(undefined);
      }
      onChanged();
    }
  }

  return <section className="execution-budget recovery-blockers" aria-label={`${stepNames[step] ?? '当前工作'}执行额度`}>
    <h3>{stepNames[step] ?? '当前工作'} · 执行额度</h3>
    {valid && workTitle.length > 0 && <p className="muted" title={current.work_title}>{workTitle.slice(0, 100).join('')}{workTitle.length > 100 ? '…' : ''}</p>}
    {!valid && <p className="hint-warning">额度记录与当前工作不一致，请重新读取后操作。</p>}
    {valid && <>
      {!known && <p className="hint-warning">用量尚未核验，暂不能追加额度或重试。请先核对原执行和用量回执。</p>}
      <div className="table-wrap"><table><thead><tr><th>额度项目</th><th>已用</th><th>上限</th><th>剩余</th><th>状态</th></tr></thead>
        <tbody>{current.dimensions.map(row => <tr key={row.key}>
          <th scope="row">{row.label}（{row.unit}）</th><td>{known ? number(row.used) : '待核验'}</td>
          <td>{number(row.limit)}</td><td>{known ? number(row.remaining) : '待核验'}</td>
          <td>{!known ? '待核验' : row.exhausted ? <strong>已耗尽{finite(row.overrun) && row.overrun > 0 ? `，已超出 ${number(row.overrun)} ${row.unit}` : ''}</strong> : '可用'}</td>
        </tr>)}</tbody></table></div>
      <p className="muted">工具调用为已观测次数；这些额度由同一工作的小步和重试累计使用。</p>
      {current.adjustment_blockers.length > 0 && <ul>{current.adjustment_blockers.map(blocker => <li key={blocker.code + blocker.message}>{blocker.message}</li>)}</ul>}
    </>}
    <ErrorBox error={readError} />
    {!open && <div className="form-actions">
      <button className="button secondary small" disabled={loading || !known || !current.can_extend}
        onClick={() => void readBudget(true)}>{loading ? '正在核对额度…' : '调整执行额度'}</button>
      <button className="button secondary small" disabled={loading} onClick={() => void readBudget(false)}>重新读取额度</button>
    </div>}
    {notice && <p role="status" className="notice">{freshRecovery ? '工作执行额度已追加，历史用量保留。请根据最新恢复条件重试。' : notice}</p>}
    {savedRevision !== undefined && <div className="form-actions"><button className="button secondary small"
      disabled={loading || Boolean(readError) || !freshRecovery || !canRetry || !known} onClick={onRetry}>重试此步骤</button>
      {!freshRecovery && <span className="muted">等待最新恢复检查…</span>}
      {freshRecovery && !canRetry && <span className="muted">仍有其他恢复条件未满足，请查看阻塞说明。</span>}
    </div>}
    {open && <form className="recovery-limit" onSubmit={extend}>
      <p>追加本工作的执行额度，已用量保留。0 表示本项不追加；这些额度独立于模型调用次数和单次输出上限。保存后重新检查恢复条件，再由你确认重试。</p>
      {dimensions.map(({ key, field, label, step: increment }) => {
        const row = snapshot?.dimensions.find(item => item.key === key);
        return <Field key={key} label={label} hint={row && finite(row.limit)
          ? `当前上限 ${number(row.limit)}；追加后 ${number(row.limit + (finite(amounts[field]) ? amounts[field] : 0))} ${row.unit}${key === 'max_active_seconds' ? '（总上限最多 86,400 秒）' : ''}` : '请重新读取当前额度'}>
          <input type="number" min="0" max={maximumAddition(key)} step={increment} required value={values[field]} disabled={Boolean(sent) || !snapshot}
            onChange={event => setValues(previous => ({ ...previous, [field]: event.target.value }))} />
        </Field>;
      })}
      <Field label="调整原因"><textarea rows={2} maxLength={2000} required value={reason} disabled={Boolean(sent) || !snapshot}
        onChange={event => setReason(event.target.value)} /></Field>
      {!sent && stale && <p className="hint-warning">状态或额度已变化，请返回并重新读取。</p>}
      {!sent && validAmounts && !enough && <p className="hint-warning">追加后，各项额度都需要有可用余额才能重试。</p>}
      {sent && command.error && <p className="muted">操作结果尚未确认。核对原提交会复用相同追加值和提交标识，不会重复追加。</p>}
      <ErrorBox error={command.error} />
      {!snapshot && <p className="hint-warning">状态已变化，请返回重新读取额度后再提交。</p>}
      <div className="form-actions"><button className="button small" disabled={command.busy || (!sent
        && (!snapshot || !known || !snapshot.can_extend || stale || !validAmounts || !enough || !reason.trim()))}>
        {command.busy ? '正在提交…' : sent ? '核对原追加提交' : '保存追加额度'}</button>
        <button type="button" className="button secondary small" disabled={command.busy || Boolean(sent)} onClick={() => setOpen(false)}>返回</button>
      </div>
    </form>}
  </section>;
}
