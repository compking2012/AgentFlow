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
