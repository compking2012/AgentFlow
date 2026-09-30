export type ReadableArtifact = {
  artifact_id: string; name: string; path?: string; media_type?: string;
  kind?: 'document' | 'code' | 'test_code'; revision?: number; digest?: string;
  download_url?: string; preview_url?: string; internal?: boolean; stale?: boolean; storage_error?: string | null;
};
export type StageTask = {
  id: string; name: string; role: string; status: string; quality_result: string;
  expected_artifact?: { name: string; kind?: string }; artifacts: ReadableArtifact[];
  blocking_reason?: string; artifact_notice?: string; is_aggregation?: boolean; is_history?: boolean;
};
export type ReviewRepairContext = {
  batch_id?: string; state: string; label: string; summary: string; reasons?: string[];
  unavailable_work_item_ids?: string[];
  tasks: (StageTask & { work_item_id: string; kind: string; step: string; status_label?: string })[];
};
export type WorkflowStage = {
  id: string; key?: string; work_item_id?: string; step: string; name: string; status: string; quality_result: string;
  expected_artifact?: { name: string; kind?: string; description?: string }; output?: ReadableArtifact | null;
  tasks: StageTask[]; dependencies?: string[]; presentation_only?: boolean;
  provenance?: { kind: 'static_baseline' | 'inherited_output'; label: string; description: string;
    source_run_id?: string | null; source_work_item_id?: string | null; artifact_id?: string | null;
    revision?: number | null; digest?: string | null };
  blocking_reason?: string; artifact_notice?: string;
  repair_context?: ReviewRepairContext;
  context?: {
    review?: { initial_work_item_id: string; initial_status: string; initial_quality_result: string;
      current_work_item_id: string; repair_round: number };
    retained_plan?: { after_repair_work_item_id: string };
  };
};
export type WorkflowPresentation = {
  run_id: string | null; input_fingerprint: string; stages: WorkflowStage[];
  product_id?: string; template_id?: string;
  version?: { change_id?: string | null; run_id?: string | null; base_run_id?: string | null; start_stage: string; kind: string };
};
export type TestSummary = {
  status: string; verified: boolean; total?: number; passed?: number; failed?: number;
  errors?: number; skipped?: number; unknown?: number; pass_rate?: number | null; duration_seconds?: number | null; coverage_complete?: boolean;
};
export type QualitySummary = {
  run_id: string; input_fingerprint: string; candidate_fingerprint?: string | null;
  unit_tests: TestSummary; integration_tests: TestSummary;
  bugs: { status: string; open?: number; resolved?: number | null; total?: number; resolution_tracking?: boolean;
    items?: { title: string; severity: string; category?: string; status: 'open' | 'resolved'; source_artifact_id?: string; path?: string }[] };
  performance: { status: string; metrics: { name: string; value: number; unit: string; sample_count?: number; source_artifact_id?: string }[] };
  paths: { project_directory?: string | null; test_directories?: string[]; delivery_directory?: string | null };
  artifacts: ReadableArtifact[];
};

export function isReadableArtifact(artifact: ReadableArtifact): boolean {
  return !artifact.internal && !artifact.stale && artifact.media_type !== 'application/json';
}

export type WorkflowTone = 'active' | 'complete' | 'attention' | 'danger' | 'pending' | 'cancelled';
type WorkflowState = Pick<WorkflowStage, 'status' | 'quality_result' | 'provenance' | 'repair_context'> & { step?: string; role?: string; tasks?: StageTask[] };
function isReview(stage: WorkflowState): boolean {
  return stage.step === 'code_review' || stage.role === 'review';
}
function currentTasks(stage: WorkflowState): StageTask[] {
  return [...(stage.tasks ?? []), ...(stage.repair_context?.tasks ?? [])].filter(task => !task.is_history);
}
export function workflowIsActive(stage: WorkflowState): boolean {
  return ['running', 'waiting_execution', 'cancel_requested'].includes(stage.status)
    || currentTasks(stage).some(task => ['running', 'waiting_execution', 'cancel_requested'].includes(task.status));
}
export function workflowHasFailure(stage: WorkflowState): boolean {
  return ['failed', 'execution_unknown'].includes(stage.status) || stage.quality_result === 'failed'
    || currentTasks(stage).some(task => ['failed', 'execution_unknown'].includes(task.status) || task.quality_result === 'failed');
}
export function workflowIsComplete(stage: WorkflowState): boolean {
  return stage.status === 'completed' && !workflowHasFailure(stage)
    && (!stage.repair_context || stage.repair_context.state === 'completed')
    && currentTasks(stage).every(task => task.status === 'completed')
    && (!isReview(stage) || (stage.quality_result === 'passed' && currentTasks(stage).every(workflowIsComplete)));
}
export function workflowTone(stage: WorkflowState): WorkflowTone {
  if (stage.status === 'inherited') return 'complete';
  if (stage.status === 'missing_baseline') return 'attention';
  if (workflowHasFailure(stage)) return 'danger';
  if (stage.repair_context?.state === 'needs_attention') return 'attention';
  if (stage.repair_context && workflowIsActive(stage)) return 'active';
  if (stage.status === 'running') return 'active';
  if (['blocked', 'waiting_approval', 'waiting_execution', 'cancel_requested', 'paused'].includes(stage.status)) return 'attention';
  if (stage.status === 'completed') return workflowIsComplete(stage) ? 'complete' : 'attention';
  if (stage.status === 'cancelled') return 'cancelled';
  return 'pending';
}

export function workflowStatus(stage: WorkflowState): string {
  if (stage.status === 'inherited') return stage.provenance?.label || '完成 · 沿用上版';
  if (stage.status === 'missing_baseline') return '缺少基线';
  if (stage.status === 'waiting_approval' && workflowHasFailure(stage)) return '待审 · 未通过';
  if (isReview(stage) && workflowHasFailure(stage) && !['failed', 'execution_unknown', 'cancelled', 'cancel_requested'].includes(stage.status)) {
    return workflowIsActive(stage) ? '审查未通过 · 仍在执行' : '审查未通过 · 待返工';
  }
  if (stage.status === 'completed' && stage.quality_result === 'failed') return '质量未通过';
  if (workflowHasFailure(stage) && !['failed', 'execution_unknown'].includes(stage.status)) return '存在失败';
  if (isReview(stage) && stage.status === 'completed' && !workflowIsComplete(stage)) return '审查待确认';
  if (stage.status === 'completed' && !workflowIsComplete(stage)) return '仍有任务未完成';
  return ({ running: '执行中', completed: '已完成', failed: '执行失败', blocked: '已阻塞',
    waiting_approval: '待人工审核', waiting_execution: '等待节点执行', execution_unknown: '待核对',
    pending: '待执行', queued: '待执行', paused: '已暂停', cancelled: '已取消', cancel_requested: '正在停止' } as Record<string, string>)[stage.status] ?? '状态待确认';
}

export function workflowContext(stage: WorkflowStage): { label?: string; summary: string } | undefined {
  if (stage.repair_context) return { label: stage.repair_context.label, summary: stage.repair_context.summary };
  const review = stage.context?.review;
  if (review) {
    const initial = review.initial_status === 'completed' && review.initial_quality_result === 'passed'
      ? '首次审查已通过' : review.initial_quality_result === 'failed' ? '首次审查未通过' : '首次审查结果待确认';
    const current = stage.quality_result === 'failed' ? '未通过' : ['failed', 'execution_unknown'].includes(stage.status)
      ? '执行失败' : workflowIsActive(stage) ? '执行中' : workflowIsComplete(stage) ? '已通过'
        : stage.status === 'completed' ? '结果待确认' : stage.status === 'waiting_approval' ? '待人工审核'
          : stage.status === 'blocked' ? '已阻塞' : stage.status === 'cancelled' ? '已取消' : '待执行';
    return { label: `修复后复审 第${review.repair_round}轮`, summary: `${initial} · 当前修复复审${current}` };
  }
  if (stage.context?.retained_plan && workflowIsComplete(stage)) return { summary: '已完成，继续沿用' };
  return undefined;
}

export type WorkflowPosition = { id: string; x: number; y: number; width: number; height: number; row: number; column: number };
export type WorkflowLink = { from: string; to: string; path: string; wrap: boolean; tone: WorkflowTone };
type Point = { x: number; y: number };
function roundedRoute(points: Point[]): string {
  const format = (point: Point) => `${point.x.toFixed(1)} ${point.y.toFixed(1)}`;
  let result = `M ${format(points[0])}`;
  for (let index = 1; index < points.length - 1; index++) {
    const previous = points[index - 1]; const corner = points[index]; const next = points[index + 1];
    const before = Math.hypot(corner.x - previous.x, corner.y - previous.y);
    const after = Math.hypot(next.x - corner.x, next.y - corner.y);
    if (!before || !after) continue;
    const radius = Math.min(10, before / 2, after / 2);
    const entry = { x: corner.x + (previous.x - corner.x) * radius / before, y: corner.y + (previous.y - corner.y) * radius / before };
    const exit = { x: corner.x + (next.x - corner.x) * radius / after, y: corner.y + (next.y - corner.y) * radius / after };
    result += ` L ${format(entry)} Q ${format(corner)} ${format(exit)}`;
  }
  return `${result} L ${format(points.at(-1)!)}`;
}

/** Fixed-size nodes and gutter routes keep every dependency outside other nodes. */
export function layoutWorkflow(stages: WorkflowStage[], availableWidth: number) {
  const width = Math.max(180, Math.floor(availableWidth));
  const padding = width < 500 ? 16 : 24; const gap = width < 500 ? 24 : 32;
  const rowGap = 48; const nodeHeight = stages.some(stage => workflowContext(stage)) ? 150 : 106;
  const columns = Math.min(5, Math.max(1, Math.floor((width - padding * 2 + gap) / (160 + gap))));
  const nodeWidth = (width - padding * 2 - gap * (columns - 1)) / columns;
  const rows = Math.max(1, Math.ceil(stages.length / columns));
  const positions = stages.map((stage, index): WorkflowPosition => ({
    id: stage.id, row: Math.floor(index / columns), column: index % columns,
    x: padding + (index % columns) * (nodeWidth + gap), y: padding + Math.floor(index / columns) * (nodeHeight + rowGap),
    width: nodeWidth, height: nodeHeight,
  }));
  const byId = new Map(positions.map(position => [position.id, position]));
  const stageById = new Map(stages.map(stage => [stage.id, stage]));
  const links: WorkflowLink[] = []; let missingDependencies = 0;
  for (const stage of stages) {
    const destination = byId.get(stage.id)!;
    for (const [offset, id] of [...new Set(stage.dependencies ?? [])].entries()) {
      const origin = byId.get(id);
      if (!origin || id === stage.id) { missingDependencies++; continue; }
      let points: Point[];
      const sourceCenter = origin.x + origin.width / 2;
      const targetCenter = destination.x + destination.width / 2;
      if (origin.row === destination.row && destination.column === origin.column + 1) {
        points = [{ x: origin.x + origin.width, y: origin.y + nodeHeight / 2 },
          { x: destination.x - 3, y: destination.y + nodeHeight / 2 }];
      } else if (origin.row === destination.row) {
        const lane = origin.y + nodeHeight + 16 + (offset % 3) * 6;
        points = [{ x: sourceCenter, y: origin.y + nodeHeight }, { x: sourceCenter, y: lane },
          { x: targetCenter, y: lane }, { x: targetCenter, y: destination.y + nodeHeight + 3 }];
      } else if (destination.row === origin.row + 1) {
        const lane = origin.y + nodeHeight + rowGap / 2 + (offset % 3 - 1) * 6;
        points = [{ x: sourceCenter, y: origin.y + nodeHeight }, { x: sourceCenter, y: lane },
          { x: targetCenter, y: lane }, { x: targetCenter, y: destination.y - 3 }];
      } else {
        const sourceLane = origin.y + nodeHeight + 18;
        const targetLane = Math.max(5, destination.y - 18);
        const gutter = width - padding / 3;
        points = [{ x: sourceCenter, y: origin.y + nodeHeight }, { x: sourceCenter, y: sourceLane },
          { x: gutter, y: sourceLane }, { x: gutter, y: targetLane },
          { x: targetCenter, y: targetLane }, { x: targetCenter, y: destination.y - 3 }];
      }
      const sourceTone = workflowTone(stageById.get(id)!);
      links.push({ from: id, to: stage.id, path: roundedRoute(points), wrap: origin.row !== destination.row,
        tone: workflowIsActive(stage) ? 'active' : sourceTone === 'complete' ? 'complete' : 'pending' });
    }
  }
  return { width, height: padding + Math.max(34, padding) + rows * nodeHeight + (rows - 1) * rowGap, columns, rows, positions, links, missingDependencies };
}
