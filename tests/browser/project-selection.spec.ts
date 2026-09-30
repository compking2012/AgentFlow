import { createRequire } from 'node:module';
import type * as Playwright from '../../apps/dashboard/node_modules/@playwright/test/index.js';
import { resolveProjectSelection } from '../../apps/dashboard/src/projectSelection';
import type { WorkflowProject } from '../../apps/dashboard/src/types';
const { test, expect } = createRequire(import.meta.url)('../../apps/dashboard/node_modules/@playwright/test/index.js') as typeof Playwright;
const projects: WorkflowProject[] = [
  { id: 'a', name: '阅读清单', product_id: 'p', project_id: 'pa', deleted: false, default_version_id: 'new', versions: [
    { id: 'initial', label: '初始开发', run_id: 'one', change_id: null, kind: 'initial', state: 'completed' },
    { id: 'old', label: '需求变更 1 · 筛选', run_id: 'two', change_id: 'c1', kind: 'change', state: 'completed' },
    { id: 'new', label: '需求变更 2 · 导出', run_id: 'three', change_id: 'c2', kind: 'change', state: 'running' },
  ] },
  { id: 'b', name: '项目二', product_id: 'q', project_id: 'pb', deleted: false, default_version_id: 'waiting', versions: [
    { id: 'waiting', label: '需求变更 1 · 待准备', run_id: null, change_id: 'pending', kind: 'change', state: 'preparing' },
  ] },
];
test('historical selection stays fixed as new changes arrive and run navigation changes project', () => {
  const choice = { projectId: 'a', versionId: 'old' };
  expect(resolveProjectSelection(projects, choice).runId).toBe('two');
  expect(resolveProjectSelection([...projects, { ...projects[1], id: 'c' }], choice).runId).toBe('two');
  expect(resolveProjectSelection(projects, { projectId: 'b' }).runId).toBe('');
  const direct = resolveProjectSelection(projects, { runId: 'one' });
  expect(direct.project?.id).toBe('a');
  expect(direct.version?.id).toBe('initial');
  expect(direct.runId).toBe('one');
});
test('explicit deleted history is retained and no-run selection cannot expose another run actions', () => {
  const deleted = [{ ...projects[0], deleted: true }, projects[1]];
  expect(resolveProjectSelection(deleted, {}).project?.id).toBe('b');
  expect(resolveProjectSelection(deleted, { runId: 'two' }).project?.id).toBe('a');
  expect(resolveProjectSelection(projects, { projectId: 'b', versionId: 'waiting' }).runId).toBe('');
  expect(resolveProjectSelection(projects, { runId: 'just-created' }).runId).toBe('just-created');
  expect(resolveProjectSelection(projects, { runId: 'just-created' }).project).toBeUndefined();
});

test('inherited nodes are visually complete without becoming completed scheduler work', async () => {
  const { workflowStatus, workflowTone, workflowIsComplete } = await import('../../apps/dashboard/src/workflow');
  const stage = { id: 'goal', step: 'goal', name: '目标整理', status: 'inherited', quality_result: 'not_applicable', tasks: [],
    provenance: { kind: 'static_baseline' as const, label: '完成 · 沿用已有项目基线', description: '静态基线' } };
  expect(workflowStatus(stage)).toBe('完成 · 沿用已有项目基线');
  expect(workflowTone(stage)).toBe('complete');
  expect(workflowIsComplete(stage)).toBe(false);
  expect(workflowStatus({ ...stage, status: 'missing_baseline' })).toBe('缺少基线');
  expect(workflowTone({ ...stage, status: 'missing_baseline' })).toBe('attention');
});
