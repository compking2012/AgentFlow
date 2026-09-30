import { useEffect, useMemo, useState } from 'react';
import type { FormEvent } from 'react';
import { OwnerApi, segment } from './api';
import { Empty, ErrorBox, Field, Metric, Panel, Status } from './components';
import { useCommand, useDialogFocus, useResource } from './hooks';
import { modelRequestLimit, roleNames, stepNames, steps, targetNames } from './labels';
import { currentQualitySummary, isConfirmedDelivery, matrixEvidence } from './quality';
import { TARGETS, stringValue } from './types';
import { ArtifactCard, ArtifactPreview, useArtifactReader } from './ArtifactViews';
import { CodeDirectories, QualityMetrics } from './QualityPanels';
import { WorkflowStages } from './WorkflowStages';
import { RecoveryControls } from './RecoveryControls';
import { LocalExecutionWaitStatus } from './LocalExecutionWaitStatus';
import type { RecoveryIntent, RecoveryOptions } from './RecoveryControls';
import { isReadableArtifact, workflowIsComplete } from './workflow';
import type { QualitySummary, WorkflowPresentation } from './workflow';
import type { Approval, Check, Entity, MatrixRow, Meta, Plan, Run, WorkItem, WorkflowProject, WorkflowVersion } from './types';

function orderItems(items: WorkItem[]) {
  return [...items].sort((a, b) => steps.indexOf(a.step) - steps.indexOf(b.step) || a.id.localeCompare(b.id));
}

function RunControls({ api, run, onChanged }: { api: OwnerApi; run: Run; onChanged: () => void }) {
  const command = useCommand(api); const [action, setAction] = useState<'pause' | 'cancel' | null>(null);
  const [reason, setReason] = useState(''); const [revision, setRevision] = useState(0);
  const finished = ['completed', 'cancelled'].includes(run.execution_state);
  useDialogFocus(Boolean(action), () => setAction(null), command.busy);
  async function submit(event: FormEvent) {
    event.preventDefault();
    if (!action) return;
    try {
      await command.execute(`/api/v1/runs/${segment(run.id)}/control`, { action, expected_revision: revision, reason: reason.trim() });
      setAction(null); onChanged();
    } catch { onChanged(); }
  }
  function open(next: 'pause' | 'cancel') { setAction(next); setRevision(run.revision); setReason(''); command.clearError(); }
  return <div className="run-controls">{run.execution_state !== 'paused' && <button className="button secondary small" disabled={finished || command.busy} onClick={() => open('pause')}>暂停运行</button>}
    <button className="button danger-quiet small" disabled={finished || command.busy} onClick={() => open('cancel')}>取消本轮</button>
    {action && <div className="modal-backdrop" onMouseDown={e => { if (e.target === e.currentTarget && !command.busy) setAction(null); }}><section role="dialog" aria-modal="true" aria-labelledby="control-title" className="modal">
      <h2 id="control-title">{action === 'cancel' ? '取消本轮运行' : action === 'pause' ? '暂停运行' : '继续运行'}</h2>
      <p className="muted">操作保留已有产物与费用记录。正在执行的动作需要到安全点停止。</p>
      <form onSubmit={submit}><Field label="操作原因"><textarea required value={reason} onChange={e => setReason(e.target.value)} rows={3} maxLength={4096} /></Field>
        {revision !== run.revision && <p className="hint-warning">运行已更新，请关闭此窗口并重新确认。</p>}
        <ErrorBox error={command.error} /><div className="form-actions"><button type="button" className="button secondary" disabled={command.busy} onClick={() => setAction(null)}>返回</button>
          <button className={`button ${action === 'cancel' ? 'danger' : ''}`} disabled={command.busy || !reason.trim() || revision !== run.revision}>{command.busy ? '正在提交…' : '确认操作'}</button></div>
      </form>
    </section></div>}
  </div>;
}

export function ExecutionView({ api, run, project, version, meta, onRefresh }: {
  api: OwnerApi; run?: Run; project?: WorkflowProject; version?: WorkflowVersion; meta?: Meta; onRefresh: () => void;
}) {
  const [recoveryIntent, setRecoveryIntent] = useState<RecoveryIntent | null>(null);
  const recovery = useResource<RecoveryOptions>(api, run ? `/api/v1/runs/${segment(run.id)}/recovery_options` : null, false, 5000);
  const emptyWorkflowPath = !run && project?.product_id && version && !version.run_id
    ? `/api/v1/products/${segment(project.product_id)}/workflow${version.change_id ? `?change_id=${segment(version.change_id)}` : ''}` : null;
  const workflow = useResource<WorkflowPresentation>(api, run ? `/api/v1/runs/${segment(run.id)}/workflow` : emptyWorkflowPath, false, 2500);
  if (!run) return <Panel title={project?.name || '执行台'} subtitle={version ? `${version.label} · 尚未创建运行` : undefined} className="workflow-panel">
    <ErrorBox error={workflow.error} onRetry={workflow.refresh} />
    {workflow.data && workflow.data.run_id === null ? <>
      <p className="muted">此版本尚未开始执行。完整流程展示已登记的项目基线和后续阶段；执行动作将在运行建立后出现。</p>
      <WorkflowStages api={api} presentation={workflow.data} />
    </> : <Empty title={workflow.loading ? '正在读取项目工作流' : '还没有选择需求变更'}>先选择项目，再选择需求变更或初始开发。</Empty>}
  </Panel>;
  const items = run.work_items ?? [];
  const active = items.filter(item => item.status === 'running').length;
  const presentation = workflow.data?.run_id === run.id && workflow.data.input_fingerprint === run.input_fingerprint ? workflow.data : undefined;
  const recoveryOptions = recovery.data && (recovery.loading ? { ...recovery.data,
    continue: { ...recovery.data.continue, eligible: false },
    retry_options: recovery.data.retry_options.map(option => ({ ...option, eligible: false })) } : recovery.data);
  const budget = run.budget_limit as { currency: string; limit_micros: number; cost_mode?: string; max_model_requests?: number } | undefined;
  return <>
    <div className="metric-grid"><Metric title="运行状态" value={<Status value={run.execution_state} />} />
      <Metric title="活跃 Agent" value={`${active} / ${meta?.agent_concurrency ?? '—'}`} detail="正在执行的任务" />
      <Metric title="待人工审核" value={run.pending_approval_count ?? items.filter(item => item.status === 'waiting_approval').length} detail="等待人工不占执行槽位" />
      <Metric title="任务完成" value={`${items.filter(workflowIsComplete).length} / ${items.length}`} detail="审查通过后计入完成；阶段交付与测试结果分别核验" /></div>
    <Panel title={run.display_name || run.goal.slice(0, 120)} subtitle="完整工作流与实时阶段状态，点击节点查看详情。" className="workflow-panel"
      action={<RunControls api={api} run={run} onChanged={() => { workflow.refresh(); onRefresh(); }} />}>
      <div className="run-facts"><span>质量结论 <Status value={run.quality_result} /></span>
        <span>成果类型 <strong>{run.purpose === 'code_delivery' ? '本地代码交付' : run.purpose === 'diagnostic' ? '诊断报告' : '阶段产物'}</strong></span>
        <span>{budget?.cost_mode === 'request_limited' ? '模型请求上限' : budget?.cost_mode === 'strict' ? '预算上限' : '费用状态'} <strong>{budget?.cost_mode === 'request_limited'
          ? `${modelRequestLimit(budget.max_model_requests)} · 金额按供应商计费` : budget?.cost_mode === 'strict' ? `${budget.currency} ${(budget.limit_micros / 1e6).toFixed(2)}` : '未提供定价依据'}</strong></span>
        {budget?.cost_mode !== 'request_limited' && budget?.max_model_requests !== undefined
          && <span>模型请求上限 <strong>{modelRequestLimit(budget.max_model_requests)}</strong></span>}</div>
      {Boolean(run.blocking_reasons?.length) && <div className="notice notice-warning"><strong>需要处理的阻塞</strong><ul>{run.blocking_reasons!.map((reason, index) => <li key={index}>{reason}</li>)}</ul></div>}
      <LocalExecutionWaitStatus key={run.id} api={api} run={run} onChanged={onRefresh} />
      {active > 0 && <div className="workflow-active-roles"><span>执行中的角色</span>{Object.entries(roleNames).filter(([role]) => role !== 'system' && items.some(item => item.role === role && item.status === 'running')).map(([role, name]) => <strong key={role}>{name} <span>{items.filter(item => item.role === role && item.status === 'running').length}</span></strong>)}</div>}
      <ErrorBox error={workflow.error ?? recovery.error} onRetry={() => { workflow.refresh(); recovery.refresh(); }} />
      <RecoveryControls api={api} run={run} options={recoveryOptions} intent={recoveryIntent} onIntent={setRecoveryIntent}
        onChanged={() => { workflow.refresh(); recovery.refresh(); onRefresh(); }} />
      {presentation ? <WorkflowStages api={api} presentation={presentation} recovery={recoveryOptions}
        onRetry={id => { const option = recoveryOptions?.retry_options.find(item => item.work_item_id === id); if (option) setRecoveryIntent({ mode: 'retry', option }); }} /> : <Empty title={workflow.loading ? '正在读取研发阶段' : '阶段视图尚未就绪'}>阶段与交付信息来自当前运行的实际记录。</Empty>}
    </Panel>
  </>;
}

export function ApprovalsView({ api, run, approvals, onRefresh }: { api: OwnerApi; run?: Run; approvals: Approval[]; onRefresh: () => void }) {
  const [snapshot, setSnapshot] = useState<Approval>(); const [decision, setDecision] = useState<'approve' | 'reject'>('approve');
  const [reason, setReason] = useState(''); const [expectation, setExpectation] = useState(''); const [returnTo, setReturnTo] = useState('');
  const [notice, setNotice] = useState(''); const command = useCommand(api);
  useDialogFocus(Boolean(snapshot), () => setSnapshot(undefined), command.busy);
  useEffect(() => { setSnapshot(undefined); setNotice(''); }, [run?.id]);
  const items = run?.work_items ?? [];
  const queue = approvals.filter(a => !a.stale && a.decision === null);
  const current = snapshot ? approvals.find(a => a.id === snapshot.id) : undefined;
  const stale = Boolean(snapshot && (!current || current.stale || current.decision !== null || current.revision !== snapshot.revision || current.fingerprint !== snapshot.fingerprint));
  const selectedItem = items.find(i => i.id === snapshot?.work_item_id);
  const ancestors = useMemo(() => {
    const found = new Set<string>();
    const visit = (id: string) => { if (found.has(id)) return; found.add(id); items.find(i => i.id === id)?.dependencies.forEach(visit); };
    if (snapshot) visit(snapshot.work_item_id);
    return orderItems(items.filter(i => found.has(i.id)));
  }, [snapshot, items]);
  function open(approval: Approval) { setSnapshot({ ...approval }); setDecision('approve'); setReason(''); setExpectation(''); setReturnTo(''); setNotice(''); command.clearError(); }
  async function submit(event: FormEvent) {
    event.preventDefault(); if (!snapshot || stale) return;
    const payload = { decision, expected_revision: snapshot.revision, expected_fingerprint: snapshot.fingerprint,
      ...(reason.trim() ? { reason: reason.trim() } : {}),
      ...(decision === 'reject' ? { change_expectation: expectation.trim(), ...(returnTo ? { return_to_work_item_id: returnTo } : {}) } : {}),
    };
    try {
      await command.execute(`/api/v1/approvals/${segment(snapshot.id)}/decisions`, payload);
      setSnapshot(undefined); setNotice(decision === 'approve' ? '审核决定已保存；质量门禁仍独立生效。' : '驳回意见已保存，相关工作将按版本重新处理。'); onRefresh();
    } catch { onRefresh(); }
  }
  return <Panel title="人工审核" subtitle="决定绑定精确产物版本。认可报告完整，不会把失败检查改为通过。" action={<span className="count-pill">{queue.length} 项待审</span>}>
    {notice && <p className="notice" role="status">{notice}</p>}
    {!run ? <Empty title="请选择一个运行">审核队列按当前运行展示。</Empty> : queue.length === 0 ? <Empty title="当前没有待审产物">这里会显示服务实际创建的审核请求。</Empty> : <div className="approval-list">{queue.map(a => {
      const item = items.find(i => i.id === a.work_item_id);
      return <article key={a.id} className="approval-card"><div><div className="eyebrow">第 {a.generation ?? item?.generation ?? '—'} 轮 · 请求版本 {a.revision}</div><h3>{stepNames[item?.step ?? ''] ?? a.work_item_id}</h3><p>质量结论 <Status value={item?.quality_result} /></p><code title={a.fingerprint}>{a.fingerprint.slice(0, 25)}…</code></div><button className="button" onClick={() => open(a)}>查看并审核</button></article>;
    })}</div>}
    {approvals.some(a => a.decision || a.stale) && <details className="advanced"><summary>审核历史（{approvals.filter(a => a.decision || a.stale).length}）</summary>{approvals.filter(a => a.decision || a.stale).map(a => <p key={a.id}><Status value={a.stale ? 'stale' : a.decision ?? undefined} /> {a.reason || '无附加意见'} <small>请求 {a.id.slice(0, 8)}</small></p>)}</details>}
    {snapshot && <div className="modal-backdrop"><section className="modal" role="dialog" aria-modal="true" aria-labelledby="approval-title">
      <div className="panel-heading"><h2 id="approval-title">审核：{stepNames[selectedItem?.step ?? ''] ?? '产物'}</h2><button className="text-button" aria-label="关闭审核" disabled={command.busy} onClick={() => setSnapshot(undefined)}>关闭</button></div>
      <div className="notice"><strong>请求版本 {snapshot.revision} · 第 {snapshot.generation ?? '—'} 轮</strong><code className="fingerprint">{snapshot.fingerprint}</code></div>
      <p>当前质量：<Status value={selectedItem?.quality_result} />。此决定只作用于这份产物。</p>
      {stale && <div className="notice notice-warning" role="alert">审核对象已更新或处理。请关闭后重新打开当前版本，不能继续批准这份快照。</div>}
      <form onSubmit={submit}><Field label="审核决定"><select value={decision} onChange={e => setDecision(e.target.value as 'approve' | 'reject')}><option value="approve">通过此产物</option><option value="reject">驳回并返工</option></select></Field>
        <Field label={decision === 'reject' ? '驳回原因' : '审核意见（可选）'}><textarea required={decision === 'reject'} rows={3} value={reason} onChange={e => setReason(e.target.value)} maxLength={8000} /></Field>
        {decision === 'reject' && <><Field label="需要怎样修改"><textarea required rows={3} value={expectation} onChange={e => setExpectation(e.target.value)} maxLength={16000} /></Field>
          <Field label="返工起点"><select value={returnTo} onChange={e => setReturnTo(e.target.value)}><option value="">此产物的生成步骤</option>{ancestors.filter(i => i.id !== snapshot.work_item_id).map(i => <option key={i.id} value={i.id}>{stepNames[i.step] ?? i.step}</option>)}</select></Field>
          <p className="hint-warning">相关产物、批准和验证将重新检查；其他无依赖模块不受影响。</p></>}
        <ErrorBox error={command.error} /><div className="form-actions"><button type="button" className="button secondary" disabled={command.busy} onClick={() => setSnapshot(undefined)}>暂不处理</button><button className="button" disabled={stale || command.busy || (decision === 'reject' && (!reason.trim() || !expectation.trim()))}>{command.busy ? '正在提交…' : '提交审核决定'}</button></div>
      </form>
    </section></div>}
  </Panel>;
}

export function QualityView({ api, run, plan, matrix, checks, candidates, deliveries }: {
  api: OwnerApi; run?: Run; plan?: Plan; matrix: MatrixRow[]; checks: Check[];
  candidates: Entity[]; deliveries: Entity[];
}) {
  const resource = useResource<QualitySummary>(api, run ? `/api/v1/runs/${segment(run.id)}/quality_summary` : null, false, 2500);
  const summary = currentQualitySummary(resource.data, run, candidates);
  const reader = useArtifactReader(api, run ? `${run.id}:${run.input_fingerprint}` : undefined);
  const documents = (summary?.artifacts ?? []).filter(isReadableArtifact)
    .filter(artifact => artifact.kind !== 'code' && artifact.kind !== 'test_code');
  const confirmed = deliveries.filter(isConfirmedDelivery);
  const evidence = matrixEvidence(run, matrix, candidates, checks);
  return <>
    <ErrorBox error={resource.error} onRetry={resource.refresh} />
    <QualityMetrics summary={summary} />
    <Panel title="平台验证" subtitle="按当前候选核验各目标，缺少证据时保持未验证。" action={<Status value={confirmed.length ? 'delivered' : 'unknown'} text={confirmed.length ? '存在已交付记录' : '尚无已确认交付'} />}>
      {!run ? <Empty title="请选择一个运行">平台矩阵和产物只展示实际运行记录。</Empty> : <div className="table-wrap"><table><thead><tr><th>测试目标</th><th>本次范围</th><th>执行结果</th><th>证据状态</th></tr></thead><tbody>{TARGETS.map(target => {
        const records = evidence.filter(item => item.app_target === target && item.required !== false);
        const declared = plan?.app_targets?.includes(target) ?? false;
        const failing = records.some(item => item.state === 'failed');
        const valid = declared && records.length > 0 && records.every(item => item.state === 'passed');
        const notRun = !records.length || records.every(item => item.state === 'not_run');
        return <tr key={target} data-testid={`target-${target}`}><td><strong>{targetNames[target]}</strong></td><td>{declared ? '已声明' : '本次未声明'}</td><td>{notRun ? <Status value="not_run" /> : <Status value={failing ? 'failed' : valid ? 'passed' : 'unknown'} text={failing ? '存在失败' : valid ? '有效通过' : '验证未完成'} />}</td><td>{records.length ? `${records.length} 项必检 · ${records.filter(item => item.state === 'passed').length} 项证据有效${notRun ? ' · 尚无执行证据' : ''}` : '尚无执行证据'}</td></tr>;
      })}</tbody></table></div>}
      {evidence.length > 0 && <details className="quality-evidence"><summary>查看必检项证据</summary><ul>{evidence.map((entry, index) => <li key={`${entry.candidateId}:${entry.matrix_entry_id}`}><strong>{targetNames[entry.app_target]} · 检查 {index + 1}</strong><Status value={entry.state} /><p>{entry.reason}</p></li>)}</ul></details>}
    </Panel>
    <CodeDirectories summary={summary} reader={reader} />
    <Panel title="研发文档与报告" subtitle="按阶段阅读或下载本轮的调研、需求、设计和验证报告。" className="artifact-documents">
      {documents.length ? <div className="artifact-list">{documents.map(artifact => <ArtifactCard key={artifact.artifact_id} artifact={artifact} reader={reader} />)}</div>
        : <Empty title="尚无已登记产物">可阅读的文档和报告生成后会出现在这里。</Empty>}
      <ArtifactPreview reader={reader} />
    </Panel>
    <div className="columns"><Panel title="候选版本"><p className="muted">源码与构建产物冻结后形成候选。</p>{candidates.length ? candidates.map(candidate => <article className="quality-version" key={candidate.id}>
      <Status value={stringValue(candidate.state ?? candidate.status)} /><dl><dt>源码提交</dt><dd><code>{stringValue(candidate.source_commit, '尚未记录')}</code></dd></dl>
    </article>) : <Empty title="暂无候选记录">等待真实源码与构建产物冻结。</Empty>}</Panel>
      <Panel title="本地交付"><p className="muted">经过确认的本地 Git 发布记录。</p>{deliveries.length ? deliveries.map(delivery => <details className="quality-delivery" key={delivery.id}>
        <summary>{isConfirmedDelivery(delivery) ? '已确认交付' : '交付尚待确认'}</summary><dl>
          <dt>交付引用</dt><dd><code>{stringValue(delivery.delivery_ref)}</code></dd><dt>源码提交</dt><dd><code>{stringValue(delivery.commit_oid)}</code></dd>
          <dt>确认时间</dt><dd>{stringValue(delivery.confirmed_at, '尚未确认')}</dd></dl>
      </details>) : <Empty title="暂无交付记录">代码验证和适用人审通过后才会发布。</Empty>}</Panel></div>
  </>;
}
