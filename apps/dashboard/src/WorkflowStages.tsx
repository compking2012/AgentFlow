import { useEffect, useId, useMemo, useRef, useState } from 'react';
import type { KeyboardEvent } from 'react';
import type { RecoveryOptions } from './RecoveryControls';
import { ArtifactCard, ArtifactPreview, useArtifactReader } from './ArtifactViews';
import { OwnerApi } from './api';
import { Empty, Status } from './components';
import { roleNames } from './labels';
import { TaskTrace } from './TaskTrace';
import { isReadableArtifact, layoutWorkflow, workflowContext, workflowIsActive, workflowIsComplete, workflowStatus, workflowTone } from './workflow';
import type { StageTask, WorkflowPresentation, WorkflowStage, WorkflowTone } from './workflow';

const linkColors: Record<WorkflowTone, string> = {
  active: '#2f8b6b', complete: '#94b2a1', pending: '#cbd7cf', attention: '#b69144', danger: '#bd7466', cancelled: '#bac4bf',
};

function recommendedStage(stages: WorkflowStage[]) {
  return stages.find(workflowIsActive) ?? stages.find(stage => workflowTone(stage) === 'danger')
    ?? stages.find(stage => workflowTone(stage) === 'attention') ?? stages[0];
}

function WorkflowStatus({ work }: { work: WorkflowStage | StageTask }) {
  const tone = workflowTone(work);
  return <Status value={tone === 'danger' ? 'failed' : tone === 'attention' ? 'waiting_approval' : work.status === 'inherited' ? 'completed' : work.status} text={workflowStatus(work)} />;
}

function TaskDetails({ api, runId, task, stageOutputId, reader }: {
  api: OwnerApi; runId: string; task: StageTask; stageOutputId?: string; reader: ReturnType<typeof useArtifactReader>;
}) {
  const artifacts = (task.artifacts ?? []).filter(isReadableArtifact);
  const separate = artifacts.filter(artifact => artifact.artifact_id !== stageOutputId);
  return <div className="workflow-map-task-body">
    <p className="workflow-map-task-quality">质量结论 <Status value={task.quality_result} /></p>
    {task.expected_artifact?.name && <p className="workflow-map-task-target"><span>目标产物</span>{task.expected_artifact.name}</p>}
    {task.blocking_reason && <p className="workflow-map-task-error">{task.blocking_reason}</p>}
    {task.artifact_notice && <p className="workflow-map-task-note">{task.artifact_notice}</p>}
    {separate.map(artifact => <ArtifactCard key={artifact.artifact_id} artifact={artifact} reader={reader} />)}
    {!separate.length && <p className="workflow-map-task-note">{artifacts.length ? '产物已列入本阶段交付。' : '暂无可阅读产物。'}</p>}
    <TaskTrace key={`${runId}:${task.id}`} api={api} runId={runId} workId={task.id} taskName={task.name} />
  </div>;
}

export function WorkflowStages({ api, presentation, recovery, onRetry }: { api: OwnerApi; presentation: WorkflowPresentation; recovery?: RecoveryOptions; onRetry?: (id: string) => void }) {
  const stages = presentation.stages;
  const scope = `${presentation.template_id ?? ""}:${presentation.version?.change_id ?? ""}:${presentation.run_id}:${presentation.input_fingerprint}`;
  const [selection, setSelection] = useState<{ scope: string; id: string | null }>();
  const [focus, setFocus] = useState<{ scope: string; id: string }>();
  const [historyFor, setHistoryFor] = useState<string>();
  const [width, setWidth] = useState(900);
  const viewport = useRef<HTMLDivElement | null>(null);
  const buttons = useRef(new Map<string, HTMLButtonElement>());
  const prefix = useId().replace(/:/g, '');
  const selected = selection?.scope === scope ? stages.find(stage => stage.id === selection.id) : undefined;
  const preferred = recommendedStage(stages);
  const focusedId = focus?.scope === scope && stages.some(stage => stage.id === focus.id) ? focus.id : preferred?.id;
  const reader = useArtifactReader(api, `${scope}:${selected?.id ?? ''}`);
  const layout = useMemo(() => layoutWorkflow(stages, width), [stages, width]);
  const active = stages.filter(workflowIsActive);
  const complete = stages.filter(stage => workflowTone(stage) === 'complete').length;
  const attention = stages.filter(stage => ['danger', 'attention'].includes(workflowTone(stage)));
  useEffect(() => {
    const element = viewport.current;
    if (!element) return;
    const update = () => { if (element.clientWidth > 0) setWidth(Math.floor(element.clientWidth)); };
    update(); const observer = new ResizeObserver(update); observer.observe(element);
    return () => observer.disconnect();
  }, [scope, Boolean(stages.length)]);

  function choose(id: string) {
    setFocus({ scope, id });
    setSelection(current => ({ scope, id: current?.scope === scope && current.id === id ? null : id }));
  }
  function navigate(event: KeyboardEvent<HTMLButtonElement>, index: number) {
    if (event.altKey || event.metaKey) return;
    if (event.key === 'Escape' && selected) { event.preventDefault(); close(); return; }
    const directions: Record<string, number> = { ArrowRight: index + 1, ArrowLeft: index - 1,
      ArrowDown: index + layout.columns, ArrowUp: index - layout.columns, Home: 0, End: stages.length - 1 };
    if (!Object.hasOwn(directions, event.key)) return;
    event.preventDefault();
    const next = stages[Math.max(0, Math.min(stages.length - 1, directions[event.key]))];
    setFocus({ scope, id: next.id }); buttons.current.get(next.id)?.focus();
  }
  function locate() {
    const choices = active.length ? active : attention;
    const current = choices.findIndex(stage => stage.id === selected?.id);
    const stage = choices[(current + 1) % choices.length];
    if (!stage) return;
    setSelection({ scope, id: stage.id }); setFocus({ scope, id: stage.id });
    const button = buttons.current.get(stage.id);
    button?.focus({ preventScroll: true }); button?.scrollIntoView({ block: 'nearest', inline: 'nearest', behavior: 'smooth' });
  }
  function close() {
    setSelection({ scope, id: null }); reader.close();
    if (selected) buttons.current.get(selected.id)?.focus({ preventScroll: true });
  }
  if (!stages.length) return <Empty title="阶段尚未生成">运行发布后会显示完整工作流与阶段交付目标。</Empty>;
  const output = selected?.output && isReadableArtifact(selected.output) ? selected.output : undefined;
  const tasks = selected?.tasks ?? [];
  const currentTasks = tasks.filter(task => !task.is_history);
  const historical = tasks.filter(task => task.is_history);
  const historyKey = `${scope}:${selected?.id}`;
  const workers = currentTasks.filter(task => !task.is_aggregation);
  const aggregations = currentTasks.filter(task => task.is_aggregation);
  const retryId = selected?.work_item_id ?? selected?.id;
  const retry = !selected?.presentation_only && recovery?.retry_options.find(option => option.work_item_id === retryId);
  const selectedContext = selected ? workflowContext(selected) : undefined;
  const blockers = [...new Set([selected?.blocking_reason, ...currentTasks.filter(task => task.blocking_reason)
    .map(task => `${task.name}：${task.blocking_reason}`)].filter((value): value is string => Boolean(value)))];

  return <div className="workflow-map">
    <header className="workflow-map-toolbar"><div><h3>研发工作流</h3><p>{stages.length} 个阶段 <span>·</span> {complete} 个完成 <span>·</span> {active.length} 个活跃</p></div>
      {(active.length > 0 || attention.length > 0) && <button className="workflow-map-locate" onClick={locate}>{active.length ? '定位执行中' : '定位待处理'} <span aria-hidden="true">↗</span></button>}
    </header>
    <div className="workflow-map-viewport" ref={viewport} role="region" aria-label="研发工作流全图" tabIndex={-1}>
      <div className="workflow-map-canvas" data-columns={layout.columns} data-rows={layout.rows} style={{ width: layout.width, height: layout.height }}>
        <svg className="workflow-map-links" width={layout.width} height={layout.height} aria-hidden="true">
          <defs>{Object.entries(linkColors).map(([tone, color]) => <marker key={tone} id={`${prefix}-${tone}`} viewBox="0 0 8 8" refX="7" refY="4" markerWidth="6" markerHeight="6" orient="auto"><path d="M 0 0 L 8 4 L 0 8 z" fill={color} /></marker>)}</defs>
          {layout.links.map(link => {
            const emphasized = selected?.id === link.from || selected?.id === link.to;
            return <path key={`${link.from}:${link.to}`} d={link.path} markerEnd={`url(#${prefix}-${link.tone})`}
              className={`workflow-map-link workflow-link-${link.tone}${emphasized ? ' workflow-link-selected' : ''}${selected && !emphasized ? ' workflow-link-muted' : ''}`}
              data-from={link.from} data-to={link.to} data-wrap={link.wrap ? 'true' : 'false'} />;
          })}
        </svg>
        <ol className="workflow-map-nodes" aria-label="研发阶段流程" aria-describedby={`${prefix}-instructions`}>
          {stages.map((stage, index) => {
            const position = layout.positions[index]; const tone = workflowTone(stage); const chosen = stage.id === selected?.id;
            const live = workflowIsActive(stage);
            const context = workflowContext(stage);
            const target = stage.expected_artifact?.name || '交付目标待明确';
            return <li key={stage.id} style={{ left: position.x, top: position.y, width: position.width, height: position.height }}>
              <button type="button" ref={element => { if (element) buttons.current.set(stage.id, element); else buttons.current.delete(stage.id); }}
                className={`workflow-map-node workflow-tone-${tone}${live ? ' workflow-node-live' : ''}${chosen ? ' workflow-node-selected' : ''}${context ? ' workflow-node-with-context' : ''}`}
                data-testid={`stage-${stage.key ?? stage.step}`} data-stage-id={stage.id} data-stage-key={stage.key ?? stage.step} data-status={stage.status} data-tone={tone} data-active={live ? 'true' : 'false'}
                aria-label={`${index + 1}. ${stage.name}，${workflowStatus(stage)}${live ? '，仍有活跃任务' : ''}。${context ? `${context.label ?? ''}，${context.summary}。` : ''}目标产物：${target}`}
                aria-pressed={chosen} aria-controls={`${prefix}-inspector`} tabIndex={stage.id === focusedId ? 0 : -1}
                title={`${stage.name}\n${target}`} onFocus={() => setFocus({ scope, id: stage.id })}
                onKeyDown={event => navigate(event, index)} onClick={() => choose(stage.id)}>
                <span className="workflow-node-top"><span className="workflow-node-number">{String(index + 1).padStart(2, '0')}</span><span className="workflow-node-state"><i aria-hidden="true" />{workflowStatus(stage)}</span></span>
                <strong className="workflow-node-name">{stage.name}</strong>
                <span className="workflow-node-output"><span className="workflow-map-sr">目标产物：</span>{target}</span>
                {context && <span className="workflow-node-context">{context.label && <b>{context.label}</b>}{context.summary}</span>}
                {live && tone !== 'active' && <small className="workflow-node-live-note"><i aria-hidden="true" />仍有活跃任务</small>}
              </button>
            </li>;
          })}
        </ol>
      </div>
    </div>
    <footer className="workflow-map-legend"><div><span><i className="workflow-legend-complete" />已完成</span><span><i className="workflow-legend-active" />执行中</span><span><i className="workflow-legend-attention" />待审 / 阻塞</span><span><i className="workflow-legend-danger" />失败 / 待核对</span></div>
      <p id={`${prefix}-instructions`}>方向键移动，Enter 或空格查看详情</p></footer>
    {layout.missingDependencies > 0 && <p className="workflow-map-missing">部分前置阶段尚未返回，连接关系暂不完整。</p>}
    <section id={`${prefix}-inspector`} className="workflow-map-inspector" aria-label="阶段详情" aria-live="polite" onKeyDown={event => {
      if (event.key === 'Escape' && selected) { event.preventDefault(); close(); }
    }}>
      {!selected ? <div className="workflow-map-prompt"><span aria-hidden="true">↗</span><div><strong>选择一个阶段</strong><p>查看阶段交付、并行任务与汇总详情。</p></div></div> : <>
        <header className="workflow-inspector-heading"><div><span className="workflow-inspector-kicker">阶段 {String(stages.indexOf(selected) + 1).padStart(2, '0')}</span><h3>{selected.name}</h3>
          <div className="workflow-inspector-states"><WorkflowStatus work={selected} />{!selected.presentation_only && <span>质量 <Status value={selected.quality_result} /></span>}{workflowIsActive(selected) && <span className="workflow-inspector-live">仍有活跃任务</span>}</div></div>
          <button className="text-button" onClick={close}>收起详情 <span aria-hidden="true">×</span></button></header>
        {selected.provenance && <div className="workflow-inspector-provenance"><strong>{selected.provenance.label}</strong><p>{selected.provenance.description}</p></div>}
        {selectedContext && <div className="workflow-inspector-context">
          {selectedContext.label && <strong>{selectedContext.label}</strong>}<p>{selectedContext.summary}</p>
          {selected.context?.retained_plan && <p>本轮局部修复保留这份已完成的测试设计，继续使用现有文档。</p>}
        </div>}
        {selected.repair_context && <section className="workflow-inspector-tasks" aria-label="审查返工详情">
          <header><h4>审查返工详情</h4><span>{selected.repair_context.label}</span></header>
          {Boolean(selected.repair_context.reasons?.length) && <div className="workflow-inspector-blockers"><strong>需要处理</strong><ul>
            {selected.repair_context.reasons!.map(reason => <li key={reason}>{reason}</li>)}
          </ul></div>}
          {Boolean(selected.repair_context.unavailable_work_item_ids?.length) && <p className="workflow-map-task-note">部分辅助任务记录尚不可核验。</p>}
          <div className="workflow-map-task-list">{selected.repair_context.tasks.map(task => <details
            className={`workflow-map-task workflow-task-tone-${workflowTone(task)}`} key={task.id}>
            <summary><span className="workflow-map-task-dot" /><span className="workflow-map-task-name"><strong>{task.name}</strong>
              <small>{roleNames[task.role] ?? task.role}</small></span><WorkflowStatus work={task} /></summary>
            <TaskDetails api={api} runId={presentation.run_id!} task={task} reader={reader} />
          </details>)}</div>
        </section>}
        {retry && <div className="form-actions"><button className="button secondary small" disabled={!retry.eligible} onClick={() => onRetry?.(retry.work_item_id)}>重新运行此步骤</button></div>}
        <p className="workflow-inspector-target"><span>目标产物</span><strong>{selected.expected_artifact?.name || '交付目标待明确'}</strong></p>
        {selected.expected_artifact?.description && <p className="workflow-inspector-description">{selected.expected_artifact.description}</p>}
        {Boolean(selected.dependencies?.length) && <p className="workflow-inspector-dependencies">前置阶段：{selected.dependencies!.map(id => stages.find(stage => stage.id === id)?.name ?? '未返回的阶段').join('、')}</p>}
        {blockers.length > 0 && <div className="workflow-inspector-blockers"><strong>需要处理</strong><ul>{blockers.map(reason => <li key={reason}>{reason}</li>)}</ul></div>}
        <div className="workflow-inspector-output"><h4>阶段交付</h4>{output ? <ArtifactCard artifact={output} reader={reader} /> : <p className="workflow-map-task-note">{selected.artifact_notice || (selected.status === 'inherited' ? '当前沿用基线；此阶段没有本次执行产物。' : '阶段交付尚未生成。')}</p>}</div>
        {!selected.presentation_only && <div className="workflow-inspector-tasks"><header><h4>{workers.length > 1 ? '并行子任务' : '执行任务'}</h4><span>{workers.filter(workflowIsComplete).length} / {workers.length} 完成</span></header>
          <div className="workflow-map-task-list">{workers.map(task => <details className={`workflow-map-task workflow-task-tone-${workflowTone(task)}`} key={task.id}>
            <summary><span className="workflow-map-task-dot" /><span className="workflow-map-task-name"><strong>{task.name}</strong><small>{roleNames[task.role] ?? task.role}</small></span><WorkflowStatus work={task} /></summary>
            {recovery?.retry_options.some(option => option.work_item_id === task.id) && <button className="button secondary small" disabled={!recovery.retry_options.find(option => option.work_item_id === task.id)?.eligible} onClick={() => onRetry?.(task.id)}>重新运行此任务</button>}
            <TaskDetails api={api} runId={presentation.run_id!} task={task} stageOutputId={output?.artifact_id} reader={reader} />
          </details>)}</div>
        </div>}
        {aggregations.length > 0 && <div className="workflow-inspector-aggregation"><h4>阶段汇总</h4>{aggregations.map(task => <div key={task.id}>
          <header><strong>{task.name}</strong><WorkflowStatus work={task} /></header><TaskDetails api={api} runId={presentation.run_id!} task={task} stageOutputId={output?.artifact_id} reader={reader} />
        </div>)}</div>}
        {historical.length > 0 && <details key={historyKey} className="workflow-inspector-history"
          onToggle={event => setHistoryFor(event.currentTarget.open ? historyKey : undefined)}>
          <summary>执行历史（{historical.length} 条）</summary>
          {historyFor === historyKey && <div className="workflow-map-task-list">{historical.map(task =>
            <details className={`workflow-map-task workflow-task-tone-${workflowTone(task)}`} key={task.id}>
              <summary><span className="workflow-map-task-name"><strong>{task.name}</strong><small>历史执行</small></span><WorkflowStatus work={task} /></summary>
              <TaskDetails api={api} runId={presentation.run_id!} task={task} stageOutputId={output?.artifact_id} reader={reader} />
            </details>)}</div>}
        </details>}
        <ArtifactPreview reader={reader} />
      </>}
    </section>
  </div>;
}
