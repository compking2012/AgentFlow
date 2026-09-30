import { spawn } from 'node:child_process';
import { mkdtemp, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { createRequire } from 'node:module';
import type * as Playwright from '../../apps/dashboard/node_modules/@playwright/test/index.js';
import type { ModelUncertainty, WorkExecutionBudget } from '../../apps/dashboard/src/types';
const { test: base, expect } = createRequire(import.meta.url)('../../apps/dashboard/node_modules/@playwright/test/index.js') as typeof Playwright;
type Server = { origin: string; bootstrap: string; run_id: string };
const repo = path.resolve(import.meta.dirname, '../..');
const test = base.extend<{ server: Server; mode: string }>({
  mode: ['failed', { option: true }],
  server: async ({ page, mode }, use) => {
    const directory = await mkdtemp(path.join(tmpdir(), 'agentflow-recovery-ui-'));
    const proc = spawn(path.join(repo, '.venv/bin/python'), ['-u', 'tests/browser/recovery_server.py', '--directory', directory, '--mode', mode], {
      cwd: repo, env: { ...process.env, PYTHONPATH: path.join(repo, 'src') }, stdio: ['ignore', 'pipe', 'pipe'],
    });
    let stderr = ''; proc.stderr.on('data', chunk => { stderr += String(chunk); });
    try {
      const server = await new Promise<Server>((resolve, reject) => {
        let output = ''; const timer = setTimeout(() => reject(new Error(stderr || 'Recovery fixture timed out')), 20000);
        proc.on('exit', code => { clearTimeout(timer); reject(new Error(`Fixture exited ${code}: ${stderr}`)); });
        proc.stdout.on('data', chunk => {
          output += String(chunk); const line = output.split('\n').find(value => value.startsWith('{"origin"'));
          if (line) { clearTimeout(timer); resolve(JSON.parse(line)); }
        });
      });
      await expect.poll(async () => { try { return (await fetch(server.origin + '/health')).status; } catch { return 0; } }).toBe(200);
      await page.goto(`${server.origin}/#bootstrap=${server.bootstrap}`);
      await expect(page.getByRole('heading', { name: '创建产品', exact: true })).toBeVisible();
      await page.getByRole('button', { name: '执行台', exact: true }).click();
      await use(server);
    } finally {
      await page.goto('about:blank'); proc.kill('SIGTERM');
      await new Promise<void>(resolve => { if (proc.exitCode !== null) resolve(); else {
        const timer = setTimeout(() => { proc.kill('SIGKILL'); resolve(); }, 5000);
        proc.once('exit', () => { clearTimeout(timer); resolve(); });
      } });
      await rm(directory, { recursive: true, force: true });
    }
  },
});

test('retry resumes the failed stage, preserves the accepted upstream result and replays a lost acknowledgement', async ({ page, server }) => {
  const requests: { key: string | undefined; body: unknown }[] = [];
  await expect(page.getByRole('button', { name: '重试中断步骤', exact: true })).toBeEnabled();
  await page.route(`**/api/v1/runs/${server.run_id}/recover`, async route => {
    requests.push({ key: route.request().headers()['idempotency-key'], body: route.request().postDataJSON() });
    const response = await route.fetch();
    if (requests.length === 1) await route.abort(); else await route.fulfill({ response });
  });
  await page.getByRole('button', { name: '重试中断步骤', exact: true }).click();
  await expect(page.getByRole('checkbox', { name: '使用当前模型设置重试', exact: true })).toHaveCount(0);
  await page.getByRole('button', { name: '确认并运行', exact: true }).click();
  await expect(page.getByRole('button', { name: '核对并重试提交', exact: true })).toBeEnabled();
  await page.getByRole('button', { name: '核对并重试提交', exact: true }).click();
  await expect(page.getByRole('dialog')).toHaveCount(0);
  expect(requests).toHaveLength(2); expect(requests[0]).toEqual(requests[1]);
  expect(requests[0].body).not.toHaveProperty('work_item_id');
  expect(requests[0].body).toMatchObject({ use_current_model_settings: false });
  await expect(page.getByRole('status').filter({ hasText: '已从保存的进度重新运行' })).toBeVisible();
  const goal = page.locator('.workflow-map-node').filter({ hasText: '目标整理' });
  await expect(goal).toContainText('已完成');
  await expect(page.locator('.workflow-map-node').filter({ hasText: '调研分析' })).toContainText('待执行');
});

test('concurrent submissions do not announce success while the original request is unresolved', async ({ page, server }) => {
  let release!: () => void; let posts = 0;
  const gate = new Promise<void>(resolve => { release = resolve; });
  await page.route(`**/api/v1/runs/${server.run_id}/recover`, async route => {
    posts += 1; await gate;
    await route.fulfill({ status: 503, contentType: 'application/json', body: JSON.stringify({ error: { code: 'temporary_failure', message: 'Fixture temporarily unavailable' } }) });
  });
  await page.getByRole('button', { name: '重试中断步骤', exact: true }).click();
  const dialog = page.getByRole('dialog');
  try {
    await dialog.locator('form').evaluate((form: HTMLFormElement) => { form.requestSubmit(); form.requestSubmit(); });
    await expect.poll(() => posts).toBe(1);
    await expect(dialog).toBeVisible();
    await expect(page.getByRole('status').filter({ hasText: '已从保存的进度重新运行' })).toHaveCount(0);
  } finally { release(); }
  await expect(dialog).toContainText('Fixture temporarily unavailable');
  await expect(dialog).toBeVisible();
});

test('current model settings require an explicit choice and remain frozen while retry is unconfirmed', async ({ page, server }) => {
  // This case verifies the coding-only UI contract; recover requests are never dispatched.
  await page.route(`**/api/v1/runs/${server.run_id}/recovery_options`, async route => {
    const response = await route.fetch();
    const options = await response.json();
    options.retry_options[0].step = 'implementation';
    await route.fulfill({ response, json: options });
  });
  await page.waitForResponse(value => value.url().endsWith(`/runs/${server.run_id}/recovery_options`));
  await page.getByRole('button', { name: '重试中断步骤', exact: true }).click();
  const dialog = page.getByRole('dialog');
  const checkbox = dialog.getByRole('checkbox', { name: '使用当前模型设置重试', exact: true });
  await expect(checkbox).not.toBeChecked();
  await expect(dialog).toContainText('更换模型或推理设置后选择；同一模型的单次输出额度自动读取配置文件。不增加本轮调用次数。');
  await checkbox.check();
  await dialog.getByRole('button', { name: '返回', exact: true }).click();
  await page.getByRole('button', { name: '重试中断步骤', exact: true }).click();
  await expect(checkbox).not.toBeChecked();
  await checkbox.check();
  const requests: { key: string | undefined; body: unknown }[] = [];
  await page.route(`**/api/v1/runs/${server.run_id}/recover`, async route => {
    requests.push({ key: route.request().headers()['idempotency-key'], body: route.request().postDataJSON() });
    await route.abort();
  });
  await dialog.getByRole('button', { name: '确认并运行', exact: true }).click();
  await expect(dialog.getByRole('button', { name: '核对并重试提交', exact: true })).toBeEnabled();
  await expect(checkbox).toBeChecked();
  await expect(checkbox).toBeDisabled();
  await dialog.getByRole('button', { name: '核对并重试提交', exact: true }).click();
  await expect.poll(() => requests.length).toBe(2);
  expect(requests[0]).toEqual(requests[1]);
  expect(requests[0].body).toMatchObject({ mode: 'retry', use_current_model_settings: true });
  await expect(dialog).toBeVisible();
  await expect(page.getByRole('status').filter({ hasText: '已从保存的进度重新运行' })).toHaveCount(0);
});

test.describe('paused work without a failure', () => {
  test.use({ mode: 'pending' });
  test('continue restarts scheduling without changing completed work', async ({ page, server }) => {
    await expect(page.getByRole('button', { name: '继续运行', exact: true })).toBeEnabled();
    const response = page.waitForResponse(value => value.url().endsWith(`/runs/${server.run_id}/recover`) && value.request().method() === 'POST');
    await page.getByRole('button', { name: '继续运行', exact: true }).click();
    await expect(page.getByRole('checkbox', { name: '使用当前模型设置重试', exact: true })).toHaveCount(0);
    await page.getByRole('button', { name: '确认并运行', exact: true }).click();
    const accepted = await response;
    expect(accepted.request().postDataJSON()).not.toHaveProperty('use_current_model_settings');
    const receipt = await accepted.json();
    expect(receipt.execution).toBe('resume_scheduling'); expect(receipt.affected_work_item_ids).toEqual([]);
    await expect(page.locator('.workflow-map-node').filter({ hasText: '目标整理' })).toContainText('已完成');
  });
});

test.describe('explicit quota recovery', () => {
  test.use({ mode: 'exhausted' });
  test('exhausted quota does not start work until the owner explicitly updates the limit', async ({ page, server }) => {
    let recoveries = 0; page.on('request', request => { if (request.url().endsWith('/recover')) recoveries += 1; });
    await expect(page.getByRole('button', { name: '重试中断步骤', exact: true })).toBeDisabled();
    await page.getByRole('button', { name: '调整本轮调用上限' }).click();
    await page.getByLabel('本轮模型调用上限', { exact: true }).fill('0');
    const updated = page.waitForResponse(response => response.url().endsWith(`/runs/${server.run_id}/request_limit`));
    await page.getByRole('button', { name: '更新本轮上限', exact: true }).click();
    const run = await (await updated).json();
    expect(run.budget_limit.max_model_requests).toBe(0); expect(run.execution_state).toBe('paused');
    expect(recoveries).toBe(0);
    await expect(page.getByRole('button', { name: '重试中断步骤', exact: true })).toBeEnabled();
  });
});

test.describe('cancelled run with exhausted quota', () => {
  test.use({ mode: 'cancelled' });
  test('can explicitly update the quota without automatically restarting cancelled work', async ({ page, server }) => {
    await expect(page.getByRole('button', { name: '调整本轮调用上限' })).toBeEnabled();
    await page.getByRole('button', { name: '调整本轮调用上限' }).click();
    await page.getByLabel('本轮模型调用上限', { exact: true }).fill('0');
    const updated = page.waitForResponse(response => response.url().endsWith(`/runs/${server.run_id}/request_limit`));
    await page.getByRole('button', { name: '更新本轮上限', exact: true }).click();
    const run = await (await updated).json();
    expect(run.execution_state).toBe('cancelled'); expect(run.budget_limit.max_model_requests).toBe(0);
    await expect(page.getByRole('button', { name: '重试中断步骤', exact: true })).toBeEnabled();
  });
});

test.describe('cancelled run can continue its remaining work', () => {
  test.use({ mode: 'cancelled-resumable' });
  test('continue restores every cancelled step and keeps the accepted goal', async ({ page, server }) => {
    const goal = page.locator('.workflow-map-node').filter({ hasText: '目标整理' });
    await expect(goal).toContainText('已完成');
    await expect(page.getByRole('button', { name: '继续运行', exact: true })).toBeEnabled();
    await page.getByRole('button', { name: '继续运行', exact: true }).click();
    await expect(page.getByRole('dialog')).toContainText('重新处理 2 项工作');
    const response = page.waitForResponse(value => value.url().endsWith(`/runs/${server.run_id}/recover`) && value.request().method() === 'POST');
    await page.getByRole('button', { name: '确认并运行', exact: true }).click();
    const accepted = await response;
    expect(accepted.request().postDataJSON()).toMatchObject({ mode: 'continue' });
    const receipt = await accepted.json();
    expect(receipt.run.execution_state).toBe('running');
    expect(receipt.affected_work_item_ids).toHaveLength(2);
    await expect(goal).toContainText('已完成');
    await expect(page.locator('.workflow-map-node').filter({ hasText: '调研分析' })).toContainText('待执行');
    await expect(page.locator('.workflow-map-node').filter({ hasText: '产品需求' })).toContainText('待执行');
    await expect(page.getByRole('status').filter({ hasText: '已继续运行，已有成果保留' })).toBeVisible();
  });
});

// These cases exercise the budget UI protocol against an isolated owner page.
// Budget/recover responses are fixtures; no scheduler or model is dispatched.
async function codingBudgetFixture(page: Playwright.Page, server: Server, mode: 'known' | 'unknown' | 'missing' = 'known') {
  const workId = 'coding-budget-work';
  const budgetPath = `/api/v1/runs/${server.run_id}/work_items/${workId}/execution_budget`;
  const state = { budget: undefined as WorkExecutionBudget | undefined, eligible: false, applied: 0,
    abortFirst: false, conflictNext: false, rejectNext: false, wrongReceipt: false, unknownAfterSave: false,
    unknownGet: false, optionsOffline: false,
    extensions: [] as { key: string; body: Record<string, unknown> }[], recoveries: [] as Record<string, unknown>[] };
  const receipts = new Map<string, WorkExecutionBudget>();
  let gate: Promise<void> | undefined;
  await page.route(`**/api/v1/runs/${server.run_id}/recovery_options`, async route => {
    if (state.optionsOffline) { await route.abort(); return; }
    const response = await route.fetch();
    const options = await response.json();
    const original = options.retry_options[0];
    if (!state.budget) state.budget = { run_id: server.run_id, work_item_id: workId,
      work_title: '编写通知模块与工单模块之间的集成测试',
      run_revision: options.expected_revision, work_revision: 1, budget_revision: mode === 'missing' ? null : 7,
      work_status: 'blocked', run_state: 'paused', metering: mode, can_extend: mode === 'known',
      adjustment_blockers: mode === 'known' ? [] : [{ code: 'coding_budget_uncertain', message: '历史执行用量待核验' }],
      dimensions: [
        { key: 'max_tool_calls', label: '工具调用次数', unit: '次', used: 31, limit: 30, remaining: 0, balance: -1, overrun: 1, exhausted: true },
        { key: 'max_active_seconds', label: '累计执行时长', unit: '秒', used: 185, limit: 300, remaining: 115, balance: 115, overrun: 0, exhausted: false },
        { key: 'max_steps', label: '小步次数', unit: '步', used: 6, limit: 8, remaining: 2, balance: 2, overrun: 0, exhausted: false },
      ].map(row => mode === 'known' ? row : { ...row, used: null, limit: mode === 'missing' ? null : row.limit,
        remaining: null, balance: null, overrun: null, exhausted: null }) as WorkExecutionBudget['dimensions'] };
    if (state.applied && gate) await gate;
    const budget = structuredClone(state.budget);
    if (state.applied && state.unknownAfterSave) {
      budget.metering = 'unknown'; budget.can_extend = false;
      budget.adjustment_blockers = [{ code: 'coding_budget_unaccounted', message: '仍有执行回执未核对' }];
      budget.dimensions = budget.dimensions.map(row => ({ ...row, used: null, remaining: null,
        balance: null, overrun: null, exhausted: null }));
    }
    options.retry_options = [{ ...original, eligible: false }, { ...original, work_item_id: workId,
      step: 'integration_test_implementation', eligible: state.eligible && !state.unknownAfterSave,
      blockers: state.eligible && !state.unknownAfterSave ? [] : [{
        code: mode === 'known' && !state.unknownAfterSave ? 'coding_budget_exhausted' : 'coding_budget_uncertain',
        message: mode === 'known' && !state.unknownAfterSave ? '本工作工具额度已耗尽' : '历史执行用量待核验' }],
      execution_budget: budget }];
    await route.fulfill({ response, json: options });
  });
  await page.route(`**${budgetPath}`, route => route.fulfill({ json: state.unknownGet ? { ...state.budget,
    metering: 'unknown', can_extend: false, adjustment_blockers: [{ code: 'coding_budget_unaccounted', message: '最新用量回执待核验' }],
    dimensions: state.budget!.dimensions.map(row => ({ ...row, used: null, remaining: null,
      balance: null, overrun: null, exhausted: null })) } : state.budget }));
  await page.route(`**${budgetPath}/extend`, async route => {
    const key = route.request().headers()['idempotency-key'];
    const body = route.request().postDataJSON();
    state.extensions.push({ key, body });
    if (state.rejectNext) {
      state.rejectNext = false;
      await route.fulfill({ status: 422, json: { error: { code: 'work_execution_limit_too_large', message: '请修正追加额度' } } });
      return;
    }
    if (state.conflictNext) {
      state.conflictNext = false; state.budget!.budget_revision! += 1;
      await route.fulfill({ status: 409, json: { error: { code: 'stale_revision', message: '额度版本已变化，请重新读取。' } } });
      return;
    }
    let receipt = receipts.get(key);
    if (!receipt) {
      const fields = { max_tool_calls: 'additional_tool_calls', max_active_seconds: 'additional_active_seconds', max_steps: 'additional_steps' };
      const budget = state.budget!;
      expect(body.expected_run_revision).toBe(budget.run_revision);
      expect(body.expected_work_revision).toBe(budget.work_revision);
      expect(body.expected_budget_revision).toBe(budget.budget_revision);
      budget.budget_revision! += 1;
      budget.dimensions = budget.dimensions.map(row => {
        const limit = row.limit! + body[fields[row.key]];
        const balance = limit - row.used!;
        return { ...row, limit, balance, remaining: Math.max(0, balance), overrun: Math.max(0, -balance), exhausted: balance <= 0 };
      });
      state.applied += 1; state.eligible = true;
      receipt = structuredClone(budget); receipts.set(key, receipt);
    }
    if (state.abortFirst && state.extensions.length === 1) { await route.abort(); return; }
    await route.fulfill({ json: state.wrongReceipt ? { ...receipt, work_item_id: 'different-work' } : receipt });
  });
  await page.route(`**/api/v1/runs/${server.run_id}/recover`, async route => {
    const body = route.request().postDataJSON(); state.recoveries.push(body);
    await route.fulfill({ json: { id: 'ui-recovery-receipt', run_id: server.run_id, mode: body.mode,
      run: { id: server.run_id, revision: state.budget!.run_revision } } });
  });
  await page.waitForResponse(response => response.url().endsWith(`/runs/${server.run_id}/recovery_options`));
  const card = page.getByRole('region', { name: '集成测试编写执行额度', exact: true });
  await expect(card).toBeVisible();
  return { state, card, workId, holdEligibility() { let release!: () => void; gate = new Promise<void>(resolve => { release = resolve; });
    return () => { gate = undefined; release(); }; } };
}

async function fillWorkExtension(card: Playwright.Locator) {
  await card.getByRole('button', { name: '调整执行额度', exact: true }).click();
  await card.getByLabel('追加工具调用次数', { exact: true }).fill('5');
  await card.getByLabel('调整原因', { exact: true }).fill('追加剩余集成测试编写所需额度');
}

test('work budget identifies the exhausted dimension and waits for fresh eligibility before retry', async ({ page, server }) => {
  const { state, card, workId, holdEligibility } = await codingBudgetFixture(page, server);
  await expect(card.getByText('编写通知模块与工单模块之间的集成测试', { exact: true })).toHaveAttribute('title', '编写通知模块与工单模块之间的集成测试');
  const tools = card.getByRole('row').filter({ hasText: '工具调用次数' });
  await expect(tools).toContainText('31'); await expect(tools).toContainText('30');
  await expect(tools).toContainText('已耗尽，已超出 1 次');
  await expect(card.getByRole('row').filter({ hasText: '累计执行时长' })).toContainText('115');
  await fillWorkExtension(card);
  const release = holdEligibility();
  try {
    await card.getByRole('button', { name: '保存追加额度', exact: true }).click();
    await expect(card.getByRole('status')).toContainText('尚未启动任务');
    await expect(card.getByRole('button', { name: '重试此步骤', exact: true })).toBeDisabled();
    expect(state.recoveries).toHaveLength(0);
  } finally { release(); }
  await expect(card.getByRole('button', { name: '重试此步骤', exact: true })).toBeEnabled();
  expect(state.budget!.dimensions[0]).toMatchObject({ used: 31, limit: 35, remaining: 4 });
  expect(state.extensions[0].body).toMatchObject({ expected_budget_revision: 7, additional_tool_calls: 5,
    additional_active_seconds: 0, additional_steps: 0 });
  await card.getByRole('button', { name: '重试此步骤', exact: true }).click();
  await page.getByRole('dialog').getByRole('button', { name: '确认并运行', exact: true }).click();
  await expect.poll(() => state.recoveries.length).toBe(1);
  expect(state.recoveries[0]).toMatchObject({ mode: 'retry', work_item_id: workId, use_current_model_settings: false });
});

for (const mode of ['unknown', 'missing'] as const) test(`work budget ${mode} values cannot be treated as zero or adjusted`, async ({ page, server }) => {
  const { card, state } = await codingBudgetFixture(page, server, mode);
  await expect(card).toContainText('用量尚未核验');
  await expect(card.getByRole('button', { name: '调整执行额度', exact: true })).toBeDisabled();
  const tools = card.getByRole('row').filter({ hasText: '工具调用次数' });
  await expect(tools.locator('td').nth(0)).toHaveText('待核验');
  await expect(tools.locator('td').nth(2)).toHaveText('待核验');
  await expect(page.getByRole('button', { name: '重试中断步骤', exact: true })).toBeDisabled();
  expect(state.extensions).toHaveLength(0); expect(state.recoveries).toHaveLength(0);
});

test('known budget extension does not bypass a newly unknown usage result', async ({ page, server }) => {
  const { card, state } = await codingBudgetFixture(page, server);
  state.unknownAfterSave = true;
  await fillWorkExtension(card);
  await card.getByRole('button', { name: '保存追加额度', exact: true }).click();
  await expect(card).toContainText('仍有执行回执未核对');
  await expect(card.getByRole('button', { name: '重试此步骤', exact: true })).toBeDisabled();
  await expect(card.getByRole('button', { name: '调整执行额度', exact: true })).toBeDisabled();
  expect(state.recoveries).toHaveLength(0);
});

test('work budget retries a lost acknowledgement with the same frozen increment and operation key', async ({ page, server }) => {
  const { state, card } = await codingBudgetFixture(page, server);
  state.abortFirst = true;
  await fillWorkExtension(card);
  await card.getByRole('button', { name: '保存追加额度', exact: true }).click();
  await expect(card.getByRole('button', { name: '核对原追加提交', exact: true })).toBeEnabled();
  state.optionsOffline = true;
  await page.waitForEvent('requestfailed', { predicate: request => request.url().endsWith(`/runs/${server.run_id}/recovery_options`), timeout: 12000 });
  await expect(card).toBeVisible();
  await expect(card.getByLabel('追加工具调用次数', { exact: true })).toBeDisabled();
  await expect(card.getByRole('status')).toHaveCount(0);
  await card.getByRole('button', { name: '核对原追加提交', exact: true }).click();
  await expect(card.getByRole('status')).toContainText('工作执行额度已追加');
  expect(state.extensions).toHaveLength(2); expect(state.extensions[0]).toEqual(state.extensions[1]);
  expect(state.applied).toBe(1); expect(state.budget!.dimensions[0].used).toBe(31);
  expect(state.recoveries).toHaveLength(0);
});

test('same-version unknown GET overrides cached known data while recovery checks are pending', async ({ page, server }) => {
  const { card, state, holdEligibility } = await codingBudgetFixture(page, server);
  await fillWorkExtension(card);
  await card.getByRole('button', { name: '保存追加额度', exact: true }).click();
  await expect(card.getByRole('button', { name: '重试此步骤', exact: true })).toBeEnabled();
  const version = state.budget!.budget_revision;
  state.unknownGet = true;
  const release = holdEligibility();
  try {
    await card.getByRole('button', { name: '重新读取额度', exact: true }).click();
    await expect(card).toContainText('最新用量回执待核验');
    await expect(card.getByRole('button', { name: '重试此步骤', exact: true })).toBeDisabled();
    await expect(card.getByRole('button', { name: '调整执行额度', exact: true })).toBeDisabled();
    expect(state.budget!.budget_revision).toBe(version);
    expect(state.recoveries).toHaveLength(0);
  } finally { state.unknownAfterSave = true; release(); }
});

test('work extension enforces cumulative time bounds and allows correcting a rejected input', async ({ page, server }) => {
  const { card, state } = await codingBudgetFixture(page, server);
  await fillWorkExtension(card);
  const seconds = card.getByLabel('追加执行时长（秒）', { exact: true });
  await seconds.fill('86300');
  await expect(card.getByRole('button', { name: '保存追加额度', exact: true })).toBeDisabled();
  expect(state.extensions).toHaveLength(0);
  await seconds.fill('0'); state.rejectNext = true;
  await card.getByRole('button', { name: '保存追加额度', exact: true }).click();
  await expect(card.getByRole('alert')).toContainText('请修正追加额度');
  await expect(card.getByLabel('追加工具调用次数', { exact: true })).toBeEnabled();
  await expect(card.getByRole('button', { name: '返回', exact: true })).toBeEnabled();
  await card.getByLabel('追加工具调用次数', { exact: true }).fill('6');
  await card.getByRole('button', { name: '保存追加额度', exact: true }).click();
  await expect(card.getByRole('status')).toContainText('工作执行额度已追加');
  expect(state.extensions[1].key).not.toBe(state.extensions[0].key);
  expect(state.applied).toBe(1); expect(state.recoveries).toHaveLength(0);
});

test('a work budget revision conflict requires a fresh form before another extension', async ({ page, server }) => {
  const { card, state } = await codingBudgetFixture(page, server);
  state.conflictNext = true;
  await fillWorkExtension(card);
  await card.getByRole('button', { name: '保存追加额度', exact: true }).click();
  await expect(card.getByRole('alert')).toContainText('额度版本已变化');
  await expect(card.getByRole('button', { name: '保存追加额度', exact: true })).toBeDisabled();
  await expect(card.getByRole('status')).toHaveCount(0);
  await card.getByRole('button', { name: '返回', exact: true }).click();
  await fillWorkExtension(card);
  await card.getByRole('button', { name: '保存追加额度', exact: true }).click();
  await expect(card.getByRole('status')).toContainText('工作执行额度已追加');
  expect(state.extensions[1].body.expected_budget_revision).toBe(8);
  expect(state.extensions[0].key).not.toBe(state.extensions[1].key);
  expect(state.applied).toBe(1); expect(state.recoveries).toHaveLength(0);
});

test('a budget receipt for another work item never reports successful adjustment', async ({ page, server }) => {
  const { card, state } = await codingBudgetFixture(page, server);
  state.wrongReceipt = true;
  await fillWorkExtension(card);
  await card.getByRole('button', { name: '保存追加额度', exact: true }).click();
  await expect(card.getByRole('alert')).toContainText('追加额度回执无法核对');
  await expect(card.getByRole('status')).toHaveCount(0);
  await expect(card.getByRole('button', { name: '核对原追加提交', exact: true })).toBeEnabled();
  expect(state.recoveries).toHaveLength(0);
});

async function uncertainModelFixture(page: Playwright.Page, server: Server, mode = 'eligible', expand = true) {
  const state = { item: undefined as ModelUncertainty | undefined, abortFirst: false, conflictNext: false,
    wrongReceipt: false, optionsOffline: false, receiptGate: undefined as Promise<void> | undefined, applied: 0, posts: [] as { key: string; body: Record<string, unknown> }[], otherMutations: [] as string[] };
  const receipts = new Map<string, ModelUncertainty>();
  const invocationId = 'interrupted-model-call';
  await page.route(`**/api/v1/runs/${server.run_id}/recovery_options`, async route => {
    if (state.optionsOffline) { await route.abort(); return; }
    const response = await route.fetch(); const options = await response.json();
    if (!state.item) state.item = { run_id: server.run_id, work_item_id: options.retry_options[0].work_item_id,
      work_title: '补全集成测试', attempt_id: 'ended-coding-attempt', invocation_id: invocationId,
      run_revision: options.expected_revision, work_revision: 2, attempt_revision: 3, invocation_revision: 4,
      attempt_budget_revision: 5, expected_state_digest: 'sha256:' + 'a'.repeat(64), state: 'uncertain',
      cost_mode: mode === 'strict' ? 'strict' : 'request_limited', usage: null, actual_micros: null, request_counted: true,
      eligible: mode !== 'active', blockers: mode === 'active' ? [{ code: 'process_active', message: '原执行仍未确认结束' }] : [],
      acknowledged: mode === 'history', acknowledgment_id: mode === 'history' ? 'historical-confirmation' : null, requires_separate_retry: true };
    const items = mode === 'history' ? Array.from({ length: 10 }, (_, index) => ({ ...state.item!,
      invocation_id: `historical-call-${index}`, work_title: `历史调用 ${index + 1}` })) : [state.item];
    options.model_uncertainties = { run_id: server.run_id, run_revision: options.expected_revision, items };
    options.retry_options = options.retry_options.map((item: Record<string, unknown>) => ({ ...item, eligible: false,
      blockers: [{ code: state.item!.acknowledged ? 'coding_budget_exhausted' : 'recovery_budget_uncertain',
        message: state.item!.acknowledged ? '工作累计时长仍已耗尽' : '存在结果未知的模型调用' }] }));
    await route.fulfill({ response, json: options });
  });
  await page.route(`**/api/v1/runs/${server.run_id}/model_uncertainties`, route => route.fulfill({ json: {
    run_id: server.run_id, run_revision: state.item!.run_revision, items: [state.item] } }));
  await page.route(`**/api/v1/runs/${server.run_id}/model_invocations/${invocationId}/acknowledge_unknown`, async route => {
    const key = route.request().headers()['idempotency-key']; const body = route.request().postDataJSON();
    state.posts.push({ key, body });
    if (state.conflictNext) {
      state.conflictNext = false; state.item!.invocation_revision += 1;
      state.item!.expected_state_digest = 'sha256:' + 'b'.repeat(64);
      await route.fulfill({ status: 409, json: { error: { code: 'revision_conflict', message: '调用记录已变化，请重新核验。' } } }); return;
    }
    let receipt = receipts.get(key);
    if (!receipt) {
      expect(body.accept_unknown_usage).toBe(true);
      expect(body.expected_state_digest).toBe(state.item!.expected_state_digest);
      state.item!.acknowledged = true; state.item!.eligible = false;
      state.item!.acknowledgment_id = 'owner-confirmed-unknown'; state.item!.invocation_revision += 1;
      state.applied += 1; receipt = structuredClone(state.item!); receipts.set(key, receipt);
    }
    if (state.abortFirst && state.posts.length === 1) { await route.abort(); return; }
    if (state.receiptGate) await state.receiptGate;
    await route.fulfill({ json: state.wrongReceipt ? { ...receipt, state: 'settled', actual_micros: 0 } : receipt });
  });
  page.on('request', request => { if (request.method() === 'POST'
    && ['/recover', '/extend', '/request_limit', '/settings/execution'].some(suffix => request.url().endsWith(suffix))) state.otherMutations.push(request.url()); });
  await page.waitForResponse(response => response.url().endsWith(`/runs/${server.run_id}/recovery_options`));
  const card = page.getByRole('region', { name: '模型调用未知用量确认', exact: true, includeHidden: true });
  const conditions = page.getByText('处理恢复条件（1）', { exact: true });
  await expect(page.locator('.recovery-controls')).toContainText(mode === 'history' ? '工作累计时长仍已耗尽' : '存在结果未知的模型调用');
  if (expand && mode !== 'history') { await conditions.click(); await expect(card).toBeVisible(); }
  return { state, card, conditions };
}

async function fillUnknownConfirmation(card: Playwright.Locator) {
  await card.getByRole('button', { name: '查看并确认未知用量', exact: true }).click();
  await card.getByRole('checkbox').check();
  await card.getByLabel('确认说明', { exact: true }).fill('已核对原执行停止，保留未知费用和原调用次数后继续检查工作额度');
}

test('unknown model usage requires explicit owner acceptance and never starts recovery or changes a budget', async ({ page, server }) => {
  const { state, card } = await uncertainModelFixture(page, server);
  await expect(page.locator('.recovery-conditions')).toContainText('可能已计费');
  await expect(page.locator('.recovery-conditions')).toContainText('不会记为零费用或已结算');
  await card.getByRole('button', { name: '查看并确认未知用量', exact: true }).click();
  await expect(card.getByRole('button', { name: '确认保留未知用量并继续核验', exact: true })).toBeDisabled();
  await card.getByRole('checkbox').check(); await card.getByLabel('确认说明', { exact: true }).fill('接受保留未知记录');
  await card.getByRole('button', { name: '确认保留未知用量并继续核验', exact: true }).click();
  await expect(card).toHaveCount(0);
  await expect(page.locator('.recovery-controls')).toContainText('工作累计时长仍已耗尽');
  await expect(page.getByRole('button', { name: '重试中断步骤', exact: true })).toBeDisabled();
  expect(state.posts[0].body).toMatchObject({ expected_work_revision: 2, expected_attempt_revision: 3,
    expected_invocation_revision: 4, expected_attempt_budget_revision: 5, accept_unknown_usage: true });
  expect(state.item).toMatchObject({ state: 'uncertain', usage: null, actual_micros: null, request_counted: true });
  expect(state.otherMutations).toEqual([]);
});

for (const mode of ['strict', 'active']) test(`unknown model usage acknowledgement stays unavailable for ${mode}`, async ({ page, server }) => {
  const { state, card } = await uncertainModelFixture(page, server, mode);
  await expect(card.getByRole('button', { name: '查看并确认未知用量', exact: true })).toBeDisabled();
  expect(state.posts).toHaveLength(0); expect(state.otherMutations).toEqual([]);
});

test('unknown usage confirmation replays a lost acknowledgement without changing the original call or risk', async ({ page, server }) => {
  const { state, card } = await uncertainModelFixture(page, server); state.abortFirst = true;
  await fillUnknownConfirmation(card);
  await card.getByRole('button', { name: '确认保留未知用量并继续核验', exact: true }).click();
  await expect(card.getByRole('button', { name: '核对原未知用量确认', exact: true })).toBeEnabled();
  await expect(card.getByRole('checkbox')).toBeDisabled(); await expect(card.getByRole('status')).toHaveCount(0);
  await card.getByRole('button', { name: '核对原未知用量确认', exact: true }).click();
  await expect(card).toHaveCount(0);
  expect(state.posts).toHaveLength(2); expect(state.posts[0]).toEqual(state.posts[1]);
  expect(state.applied).toBe(1); expect(state.otherMutations).toEqual([]);
});

test('unknown usage confirmation needs a new snapshot after a revision conflict', async ({ page, server }) => {
  const { state, card } = await uncertainModelFixture(page, server); state.conflictNext = true;
  await fillUnknownConfirmation(card);
  await card.getByRole('button', { name: '确认保留未知用量并继续核验', exact: true }).click();
  await expect(card.getByRole('alert')).toContainText('调用记录已变化');
  await expect(card.getByRole('button', { name: '确认保留未知用量并继续核验', exact: true })).toBeDisabled();
  await card.getByRole('button', { name: '返回', exact: true }).click();
  await fillUnknownConfirmation(card);
  await card.getByRole('button', { name: '确认保留未知用量并继续核验', exact: true }).click();
  await expect(card).toHaveCount(0);
  expect(state.posts[1].body.expected_invocation_revision).toBe(5);
  expect(state.posts[0].key).not.toBe(state.posts[1].key); expect(state.applied).toBe(1);
});

test('a response that settles an unknown call is not accepted as a risk-preserving acknowledgement', async ({ page, server }) => {
  const { state, card } = await uncertainModelFixture(page, server); state.wrongReceipt = true;
  await fillUnknownConfirmation(card);
  await card.getByRole('button', { name: '确认保留未知用量并继续核验', exact: true }).click();
  await expect(card.getByRole('alert')).toContainText('确认回执无法核对');
  await expect(card.getByRole('status')).toHaveCount(0);
  await expect(card.getByRole('button', { name: '核对原未知用量确认', exact: true })).toBeEnabled();
  expect(state.otherMutations).toEqual([]);
});

test('an unknown-usage acknowledgement cannot hide later invocation evidence invalidation', async ({ page, server }) => {
  const { state, card } = await uncertainModelFixture(page, server);
  await fillUnknownConfirmation(card);
  await card.getByRole('button', { name: '确认保留未知用量并继续核验', exact: true }).click();
  await expect(card).toHaveCount(0);
  state.item!.acknowledged = false; state.item!.eligible = false; state.item!.invocation_revision += 1;
  state.item!.expected_state_digest = 'sha256:' + 'c'.repeat(64);
  state.item!.blockers = [{ code: 'acknowledgment_stale', message: '旧确认不再匹配当前调用记录' }];
  await page.waitForResponse(async response => response.url().endsWith(`/runs/${server.run_id}/recovery_options`)
    && (await response.json()).model_uncertainties?.items[0]?.expected_state_digest === 'sha256:' + 'c'.repeat(64));
  await expect(card).toContainText('旧确认不再匹配当前调用记录');
  await expect(card.getByRole('status')).toHaveCount(0);
  await expect(card.getByRole('button', { name: '查看并确认未知用量', exact: true })).toBeDisabled();
  expect(state.posts).toHaveLength(1); expect(state.otherMutations).toEqual([]);
});

test('ten acknowledged historical calls add no recovery warning cards or handling entry', async ({ page, server }) => {
  const { state, card } = await uncertainModelFixture(page, server, 'history', false);
  await expect(card).toHaveCount(0);
  await expect(page.getByText(/^处理恢复条件（/)).toBeHidden();
  await expect(page.getByText('模型调用用量待确认', { exact: true })).toHaveCount(0);
  await page.screenshot({ path: test.info().outputPath('confirmed-history.png'), fullPage: true });
  await expect(page.locator('.recovery-controls')).toContainText('工作累计时长仍已耗尽');
  await expect(page.getByRole('button', { name: '重试中断步骤', exact: true })).toBeDisabled();
  expect(state.posts).toHaveLength(0); expect(state.otherMutations).toEqual([]);
});

test('pending usage offers one collapsed entry and preserves an unfinished form when collapsed', async ({ page, server }) => {
  const { state, card, conditions } = await uncertainModelFixture(page, server, 'eligible', false);
  await expect(conditions).toBeVisible();
  await expect(card).toBeHidden();
  await expect(page.getByText('模型调用用量待确认', { exact: true })).toHaveCount(0);
  await page.screenshot({ path: test.info().outputPath('collapsed-usage-entry.png'), fullPage: true });
  expect(state.posts).toHaveLength(0);
  await conditions.click();
  await fillUnknownConfirmation(card);
  const reason = await card.getByLabel('确认说明', { exact: true }).inputValue();
  await conditions.click();
  await expect(card).toBeHidden();
  await conditions.click();
  await expect(card.getByRole('checkbox')).toBeChecked();
  await expect(card.getByLabel('确认说明', { exact: true })).toHaveValue(reason);
  await expect(card.getByRole('button', { name: '确认保留未知用量并继续核验', exact: true })).toBeEnabled();
  expect(state.posts).toHaveLength(0); expect(state.otherMutations).toEqual([]);
});

test('a lost usage acknowledgement keeps its pending form across refreshed acknowledged history and collapse', async ({ page, server }) => {
  const { state, card, conditions } = await uncertainModelFixture(page, server); state.abortFirst = true;
  await fillUnknownConfirmation(card);
  const reason = await card.getByLabel('确认说明', { exact: true }).inputValue();
  await card.getByRole('button', { name: '确认保留未知用量并继续核验', exact: true }).click();
  await expect(card.getByRole('button', { name: '核对原未知用量确认', exact: true })).toBeEnabled();
  await page.waitForResponse(response => response.url().endsWith(`/runs/${server.run_id}/recovery_options`));
  await expect(conditions).toBeVisible();
  await conditions.click(); await expect(card).toBeHidden(); await conditions.click();
  await expect(card.getByLabel('确认说明', { exact: true })).toHaveValue(reason);
  await expect(card.getByRole('checkbox')).toBeChecked();
  await expect(card.getByRole('checkbox')).toBeDisabled();
  await card.getByRole('button', { name: '核对原未知用量确认', exact: true }).click();
  await expect(card).toHaveCount(0); await expect(conditions).toBeHidden();
  expect(state.posts).toHaveLength(2); expect(state.posts[0]).toEqual(state.posts[1]);
  expect(state.applied).toBe(1); expect(state.otherMutations).toEqual([]);
});

test('new server evidence wins when an old acknowledgement receipt arrives after it', async ({ page, server }) => {
  const { state, card, conditions } = await uncertainModelFixture(page, server);
  await fillUnknownConfirmation(card);
  let release!: () => void;
  state.receiptGate = new Promise<void>(resolve => { release = resolve; });
  try {
    await card.getByRole('button', { name: '确认保留未知用量并继续核验', exact: true }).click();
    await expect.poll(() => state.applied).toBe(1);
    state.item!.acknowledged = false; state.item!.eligible = true; state.item!.invocation_revision += 1;
    state.item!.expected_state_digest = 'sha256:' + 'd'.repeat(64);
    const refreshed = await page.waitForResponse(async response => response.url().endsWith(`/runs/${server.run_id}/recovery_options`)
      && (await response.json()).model_uncertainties?.items[0]?.expected_state_digest === 'sha256:' + 'd'.repeat(64));
    expect((await refreshed.json()).model_uncertainties.items[0].acknowledged).toBe(false);
    state.optionsOffline = true;
  } finally { release(); }
  await expect(card.getByRole('button', { name: '查看并确认未知用量', exact: true })).toBeEnabled();
  await expect(conditions).toBeVisible();
  await expect(card.getByRole('status')).toHaveCount(0);
  expect(state.posts).toHaveLength(1); expect(state.otherMutations).toEqual([]);
});

test('switching runs resets the usage entry and never carries the prior run confirmation form', async ({ page, server }) => {
  const { state, card } = await uncertainModelFixture(page, server);
  await fillUnknownConfirmation(card);
  const secondId = 'second-uncertain-run';
  await page.route('**/api/v1/project_workflows', async route => {
    const response = await route.fetch(); const value = await response.json();
    const item = value.items.find((project: { versions: { run_id: string }[] }) => project.versions.some(version => version.run_id === server.run_id));
    item.versions.push({ id: 'run:' + secondId, run_id: secondId, change_id: null, label: '另一运行', kind: 'run', state: 'paused' });
    await route.fulfill({ response, json: value });
  });
  await page.route(`**/api/v1/runs/${secondId}`, async route => {
    const response = await route.fetch({ url: `${server.origin}/api/v1/runs/${server.run_id}` });
    await route.fulfill({ response, json: { ...await response.json(), id: secondId, goal: '另一运行', display_name: '另一运行' } });
  });
  await page.route(`**/api/v1/runs/${secondId}/**`, async route => {
    const suffix = new URL(route.request().url()).pathname.split(`/${secondId}/`)[1];
    if (suffix === 'recovery_options') {
      await route.fulfill({ json: { run_id: secondId, expected_revision: state.item!.run_revision,
        continue: { eligible: false, blockers: [] }, retry_options: [], model_uncertainties: {
          run_id: secondId, run_revision: state.item!.run_revision,
          items: [{ ...state.item!, run_id: secondId, work_title: '另一运行的待确认调用' }] } } });
    } else if (suffix.startsWith('events')) await route.abort();
    else await route.fulfill({ json: { items: [] } });
  });
  await page.getByRole('button', { name: '刷新数据', exact: true }).click();
  await expect(page.getByRole('combobox', { name: '需求变更', exact: true }).locator(`option[value="run:${secondId}"]`)).toHaveCount(1);
  await page.getByRole('combobox', { name: '需求变更', exact: true }).selectOption('run:' + secondId);
  await expect(page.getByRole('heading', { name: '另一运行', exact: true })).toBeVisible();
  const conditions = page.getByText('处理恢复条件（1）', { exact: true });
  await expect(conditions).toBeVisible(); await expect(card).toBeHidden();
  await conditions.click();
  await expect(card).toContainText('另一运行的待确认调用');
  await expect(card.getByRole('checkbox')).toHaveCount(0);
  await expect(card.getByRole('button', { name: '查看并确认未知用量', exact: true })).toBeEnabled();
  expect(state.posts).toHaveLength(0); expect(state.otherMutations).toEqual([]);
});
