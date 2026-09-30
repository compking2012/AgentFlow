import type * as Playwright from '../../apps/dashboard/node_modules/@playwright/test/index.js';
import { createRequire } from 'node:module';
import { workflowContext, workflowIsActive, workflowIsComplete, workflowStatus, workflowTone } from '../../apps/dashboard/src/workflow';
import type { WorkflowStage } from '../../apps/dashboard/src/workflow';

const { test, expect } = createRequire(import.meta.url)('../../apps/dashboard/node_modules/@playwright/test/index.js') as typeof Playwright;

test('review repair stays in one failed review node while auxiliary work is active', () => {
  const stage: WorkflowStage = { id: 'review', step: 'code_review', name: '代码审查', status: 'completed', quality_result: 'failed', tasks: [],
    repair_context: { state: 'repairing', label: '审查返工', summary: '测试迁移 1 项', tasks: [
      { id: 'migration', work_item_id: 'migration', kind: 'test_contract_migration', name: '测试迁移', step: 'integration_test_implementation',
        status: 'running', quality_result: 'unknown', role: 'integration_test', artifacts: [] },
    ] } };
  expect(workflowIsActive(stage)).toBe(true);
  expect(workflowTone(stage)).toBe('danger');
  expect(workflowIsComplete(stage)).toBe(false);
  expect(workflowStatus(stage)).toContain('审查未通过');
  expect(workflowContext(stage)).toEqual({ label: '审查返工', summary: '测试迁移 1 项' });
  stage.repair_context!.tasks[0].status = 'pending';
  expect(workflowIsActive(stage)).toBe(false);
  expect(workflowStatus(stage)).toBe('审查未通过 · 待返工');
});

test('completed disposition is auxiliary work, not an unconfirmed code review', () => {
  const stage: WorkflowStage = { id: 'review', step: 'code_review', name: '代码审查',
    status: 'completed', quality_result: 'passed', tasks: [],
    repair_context: { state: 'completed', label: '审查返工已结束', summary: '生产修复 1 项', tasks: [
      { id: 'triage', work_item_id: 'triage', kind: 'triage', name: '归属分析', step: 'review_disposition',
        role: 'review', status: 'completed', quality_result: 'unknown', artifacts: [] },
      { id: 'reviewer', work_item_id: 'reviewer', kind: 'review', name: '独立复审', step: 'code_review',
        role: 'review', status: 'completed', quality_result: 'passed', artifacts: [] },
    ] } };
  expect(workflowIsComplete(stage)).toBe(true);
  expect(workflowTone(stage)).toBe('complete');
  expect(workflowStatus(stage)).toBe('已完成');
  expect(workflowStatus(stage.repair_context!.tasks[0])).toBe('已完成');
  stage.repair_context!.tasks[1].quality_result = 'unknown';
  expect(workflowIsComplete(stage)).toBe(false);
  expect(workflowStatus(stage)).toBe('审查待确认');
  stage.repair_context!.tasks[1].quality_result = 'failed';
  expect(workflowStatus(stage)).toBe('审查未通过 · 待返工');
});

test('unfinished or failed disposition still prevents completed review presentation', () => {
  const stage: WorkflowStage = { id: 'review', step: 'code_review', name: '代码审查',
    status: 'completed', quality_result: 'passed', tasks: [],
    repair_context: { state: 'completed', label: '审查返工', summary: '', tasks: [
      { id: 'triage', work_item_id: 'triage', kind: 'triage', name: '归属分析', step: 'review_disposition',
        role: 'review', status: 'running', quality_result: 'unknown', artifacts: [] },
    ] } };
  expect(workflowIsComplete(stage)).toBe(false);
  expect(workflowIsActive(stage)).toBe(true);
  stage.repair_context!.tasks[0].status = 'completed';
  stage.repair_context!.tasks[0].quality_result = 'failed';
  expect(workflowIsComplete(stage)).toBe(false);
  expect(workflowStatus(stage)).toBe('审查未通过 · 待返工');
});
