import type * as Playwright from '../../apps/dashboard/node_modules/@playwright/test/index.js';
import { createRequire } from 'node:module';
import { spawn } from 'node:child_process';
import { mkdtemp, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import path from 'node:path';
import type { ExecutionSettingField, ExecutionSettings } from '../../apps/dashboard/src/types';

const { test: base, expect } = createRequire(import.meta.url)('../../apps/dashboard/node_modules/@playwright/test/index.js') as typeof Playwright;
const repository = path.resolve(import.meta.dirname, '../..');
type Server = { origin: string; bootstrap: string };
const test = base.extend<{ server: Server }>({
  server: async ({ page }, use) => {
    const directory = await mkdtemp(path.join(tmpdir(), 'agentflow-execution-settings-ui-'));
    const child = spawn(path.join(repository, '.venv/bin/python'), ['-u', 'tests/browser/product_server.py', '--directory', directory], {
      cwd: repository, env: { ...process.env, PYTHONPATH: path.join(repository, 'src') }, stdio: ['ignore', 'pipe', 'pipe'],
    });
    let errors = ''; child.stderr.on('data', value => { errors += String(value); });
    try {
      const server = await new Promise<Server>((resolve, reject) => {
        let output = ''; const timer = setTimeout(() => reject(new Error(errors || 'Settings fixture timed out')), 20000);
        child.once('exit', code => { clearTimeout(timer); reject(new Error(`Fixture exited ${code}: ${errors}`)); });
        child.stdout.on('data', value => {
          output += String(value); const line = output.split('\n').find(item => item.startsWith('{"origin"'));
          if (line) { clearTimeout(timer); resolve(JSON.parse(line)); }
        });
      });
      await expect.poll(async () => { try { return (await fetch(server.origin + '/health')).status; } catch { return 0; } }).toBe(200);
      await use(server);
    } finally {
      await page.goto('about:blank').catch(() => undefined); child.kill('SIGTERM');
      await new Promise<void>(resolve => { if (child.exitCode !== null) resolve(); else {
        const timer = setTimeout(() => { child.kill('SIGKILL'); resolve(); }, 5000);
        child.once('exit', () => { clearTimeout(timer); resolve(); });
      } });
      await rm(directory, { recursive: true, force: true });
    }
  },
});

const definitions: [string, string, number | boolean, string, number | null, number | null, string | null, 'common' | 'advanced', boolean?][] = [
  ['product.max_model_requests', '每轮模型调用上限', 200, '次', 0, 2000, '不限调用次数', 'common'],
  ['product.max_tool_calls', '每工作工具调用上限', 100, '次', 0, null, '没有工具调用额度', 'common'],
  ['product.max_active_seconds', '每工作累计执行时长', 1800, '秒', null, 86400, null, 'common', false],
  ['app.agent_concurrency', '同时执行的 Agent 数', 3, '个', 1, 32, null, 'common'],
  ['app.max_coding_steps', '每工作编码小步上限', 32, '步', 1, 128, null, 'advanced'],
  ['app.auto_failure_retry_limit', '每工作自动恢复上限', 100, '轮', 0, 100, '关闭自动恢复', 'advanced'],
  ['app.auto_failure_run_limit', '每轮自动恢复总上限', 100, '次', 0, 100, '关闭自动恢复', 'advanced'],
  ['app.auto_timeout_retry_limit', '超时自动续跑次数', 100, '次', 0, 100, '关闭超时自动续跑', 'advanced'],
  ['app.auto_failure_retry_delay_seconds', '自动恢复等待时间', 30, '秒', 0, 3600, '不等待', 'advanced'],
  ['app.auto_review_repair_limit', '审查自动返工上限', 100, '轮', -1, null, '关闭自动返工', 'advanced'],
  ['app.auto_test_repair_limit', '测试失败自动修复次数', 100, '次', -1, null, '关闭测试自动修复', 'advanced'],
  ['app.max_role_iterations', '专业角色执行轮次', 1000, '轮', 1, null, null, 'advanced'],
  ['app.research_public_web_enabled', '公开网页调研', true, '开关', null, null, null, 'advanced', false],
  ['app.node_active_seconds', '节点作业执行时长', 1800, '秒', 30, 86400, null, 'advanced'],
];

// Local browser protocol fixtures; file persistence and owner authorization are
// exercised by backend tests. This suite never touches the owner's real TOML.
async function setup(page: Playwright.Page, server: Server, initial: Record<string, number | boolean> = {}) {
  let revision = 1;
  const saved = { ...Object.fromEntries(definitions.map(([key, , value]) => [key, value])), ...initial };
  const loaded = { ...saved };
  const fingerprint = () => 'sha256:' + revision.toString(16).padStart(64, '0');
  const state = { gets: 0, applied: 0, abortFirst: false, conflictNext: false, rejectNext: false, unconfirmed: false,
    durabilityFailureStatus: undefined as number | undefined,
    requests: [] as { key: string; body: { expected_configuration_revision: string; values: Record<string, number | boolean> } }[],
    forbidden: [] as string[], saved,
    external(key: string, value: number) { saved[key] = value; revision += 1; },
    view(): ExecutionSettings {
      const fields: ExecutionSettingField[] = definitions.map(([key, label, default_value, unit, minimum, maximum, zero_meaning, group, integer = true]) => ({
        key, label, default_value, unit, minimum, maximum, exclusive_minimum: key === 'product.max_active_seconds' ? 0 : null,
        zero_meaning, group, integer, boolean: typeof default_value === 'boolean', source: 'configuration_file', saved_value: saved[key], loaded_value: loaded[key],
        restart_required: saved[key] !== loaded[key], restart_on_change: true,
        description: key === 'product.max_active_seconds' ? '同一工作的所有小步与重试累计计时。' : '以配置的执行策略为准。',
        effect_scope: '重启后用于后续执行；现有运行保持原快照。',
      }));
      return { configuration_path: '/fixture/private/config.toml', configuration_revision: fingerprint(),
        restart_required: fields.some(field => field.restart_required), saved_values: { ...saved }, loaded_values: { ...loaded }, fields,
        current_runs_changed: false, model_parameters: { source: 'model_configuration', description: '已派发任务保留原模型授权。',
          roles: { model: 'fixture-role-model', max_output_tokens: 65536, source: 'models.roles', configured: true },
          coding: { model: 'fixture-coding-model', max_output_tokens: 131072, source: 'models.coding', configured: true } } };
    } };
  const receipts = new Map<string, ExecutionSettings>();
  page.on('request', request => { if (request.method() !== 'GET' && ['/recover', '/request_limit', '/extend', '/product_setup/models']
    .some(suffix => request.url().endsWith(suffix))) state.forbidden.push(request.url()); });
  await page.route('**/api/v1/settings/execution', async route => {
    if (route.request().method() === 'GET') { state.gets += 1; await route.fulfill({ json: state.view() }); return; }
    const key = route.request().headers()['idempotency-key']; const body = route.request().postDataJSON();
    state.requests.push({ key, body });
    if (state.unconfirmed) {
      await route.fulfill({ status: 409, json: { error: { code: 'execution_settings_save_unconfirmed',
        message: '当前文件已保留，原保存结果无法确认。', details: { save_state: 'unconfirmed', requires_refresh: true } } } }); return;
    }
    if (state.rejectNext) { state.rejectNext = false;
      await route.fulfill({ status: 422, json: { error: { code: 'configuration_invalid', message: '请修正执行设置输入。' } } }); return; }
    if (state.conflictNext) { state.conflictNext = false; state.external('app.node_active_seconds', 900); }
    let receipt = receipts.get(key);
    if (!receipt) {
      if (body.expected_configuration_revision !== fingerprint()) {
        await route.fulfill({ status: 409, json: { error: { code: 'configuration_changed', message: '文件已被其他编辑修改。' } } }); return;
      }
      expect(Object.keys(body.values).every(name => name in saved && !name.startsWith('models.'))).toBe(true);
      Object.assign(saved, body.values); revision += 1; state.applied += 1;
      receipt = { ...state.view(), operation_id: `saved-${state.applied}`, saved_at: '2026-09-24T00:00:00Z' };
      receipts.set(key, receipt);
    }
    if (state.abortFirst && state.requests.length === 1) { await route.abort(); return; }
    if (state.durabilityFailureStatus !== undefined) {
      const status = state.durabilityFailureStatus; state.durabilityFailureStatus = undefined;
      await route.fulfill({ status, json: { error: { code: 'configuration_unwritable', message: '文件持久化尚未确认，请核对原保存。' } } }); return;
    }
    await route.fulfill({ json: receipt });
  });
  await page.goto(`${server.origin}/#bootstrap=${encodeURIComponent(server.bootstrap)}`);
  await expect(page.getByRole('heading', { name: '创建产品', exact: true })).toBeVisible();
  expect(state.gets).toBe(0);
  await page.getByRole('button', { name: '本地设置', exact: true }).click();
  const panel = page.locator('.execution-settings');
  await expect(panel.getByRole('form', { name: '执行设置', exact: true })).toBeVisible();
  return { state, panel, form: panel.getByRole('form', { name: '执行设置', exact: true }) };
}

test('execution settings expose limits and model sources, then save only changed application fields', async ({ page, server }, testInfo) => {
  const { panel, form, state } = await setup(page, server);
  await expect(panel).toContainText('models.roles.max_output_tokens');
  await expect(panel).toContainText('65,536 token'); await expect(panel).toContainText('131,072 token');
  await expect(form).toContainText('文件值 100；本次已加载 100；默认 100 次');
  await expect(form).toContainText('0：没有工具调用额度');
  await expect(form).toContainText('0：不限调用次数');
  await form.getByLabel('每工作工具调用上限（次）', { exact: true }).fill('180');
  await form.getByLabel('每工作累计执行时长（秒）', { exact: true }).fill('2400');
  await form.getByText('高级执行策略', { exact: true }).click();
  await form.getByLabel('同时执行的 Agent 数（个）', { exact: true }).fill('5');
  await expect(form).toContainText('0：关闭自动恢复'); await expect(form).toContainText('0：不等待');
  await expect(form.getByLabel('专业角色执行轮次（轮）', { exact: true })).toHaveValue('1000');
  await form.getByRole('button', { name: '保存执行设置', exact: true }).click();
  await expect(form.getByRole('status')).toContainText('已有运行的额度和已用量保持不变');
  await expect(panel).toContainText('需要重启平台后生效');
  await expect(form).toContainText('文件值 180；本次已加载 100；默认 100 次');
  await panel.screenshot({ path: testInfo.outputPath('execution-settings.png') });
  expect(state.requests[0].body.values).toEqual({ 'product.max_tool_calls': 180, 'product.max_active_seconds': 2400, 'app.agent_concurrency': 5 });
  expect(state.forbidden).toEqual([]);
  await panel.getByRole('button', { name: '前往模型设置', exact: true }).click();
  await expect(page.getByRole('form', { name: '设置研发模型', exact: true })).toBeVisible();
  await page.getByRole('button', { name: '本地设置', exact: true }).click();
  await panel.getByRole('button', { name: '前往执行台调整当前工作额度', exact: true }).click();
  await expect(page.getByRole('heading', { name: '执行台', exact: true, level: 1 })).toBeVisible();
  expect(state.forbidden).toEqual([]);
});

test('continuous review repair permits saving other fields and respects the server numeric range', async ({ page, server }) => {
  const { form, state } = await setup(page, server, { 'app.auto_review_repair_limit': -1 });
  const save = form.getByRole('button', { name: '保存执行设置', exact: true });
  const tools = form.getByLabel('每工作工具调用上限（次）', { exact: true });
  await tools.fill('180');
  await expect(save).toBeEnabled();
  await form.getByText('高级执行策略', { exact: true }).click();
  const review = form.getByLabel('审查自动返工上限（轮）', { exact: true });
  const field = form.locator('.execution-setting-field').filter({ hasText: 'app.auto_review_repair_limit' });
  await expect(review).toHaveValue('-1');
  await expect(field).toContainText('文件值 持续修复；本次已加载 持续修复；默认 100 轮');
  await review.fill('-2'); await expect(save).toBeDisabled();
  await review.fill('-1');
  await tools.fill('-1'); await expect(save).toBeDisabled();
  await tools.fill('180');
  await review.fill('8'); await expect(save).toBeEnabled();
  await save.click();
  await expect(form.getByRole('status')).toContainText('本次保存已确认');
  expect(state.requests[0].body.values).toEqual({ 'product.max_tool_calls': 180, 'app.auto_review_repair_limit': 8 });
  await review.fill('-1'); await expect(save).toBeEnabled();
  await save.click();
  await expect(field).toContainText('文件值 持续修复');
  expect(state.requests[1].body.values).toEqual({ 'app.auto_review_repair_limit': -1 });
  expect(state.forbidden).toEqual([]);
});

test('timeout retry policy can be disabled without starting or resetting a run', async ({ page, server }) => {
  const { form, state } = await setup(page, server);
  await form.getByText('高级执行策略', { exact: true }).click();
  await expect(form.getByLabel('超时自动续跑次数（次）', { exact: true })).toHaveValue('100');
  await form.getByLabel('超时自动续跑次数（次）', { exact: true }).fill('0');
  await expect(form).toContainText('0：关闭超时自动续跑');
  await form.getByRole('button', { name: '保存执行设置', exact: true }).click();
  await expect(form.getByRole('status')).toContainText('本次保存已确认');
  expect(state.requests[0].body.values).toEqual({ 'app.auto_timeout_retry_limit': 0 });
  expect(state.forbidden).toEqual([]);
});

test('unsaved execution edits survive refresh and navigation; external changes require explicit comparison', async ({ page, server }) => {
  const { panel, form, state } = await setup(page, server);
  const tools = form.getByLabel('每工作工具调用上限（次）', { exact: true });
  await tools.fill('180'); state.external('app.agent_concurrency', 4);
  await panel.getByRole('button', { name: '重新读取执行设置', exact: true }).click();
  await expect(form).toContainText('配置文件已变化，你的输入已保留');
  await expect(tools).toHaveValue('180');
  await page.getByRole('button', { name: '执行台', exact: true }).click();
  await page.getByRole('button', { name: '本地设置', exact: true }).click();
  await expect(tools).toHaveValue('180');
  await expect(form.getByRole('button', { name: '保存执行设置', exact: true })).toBeDisabled();
  await form.getByRole('button', { name: '核对外部修改', exact: true }).click();
  await expect(form.getByRole('row').filter({ hasText: '同时执行的 Agent 数' })).toContainText('未修改');
  await form.getByRole('button', { name: '采用当前文件版本并保留我的修改', exact: true }).click();
  expect(state.requests).toHaveLength(0);
  await form.getByRole('button', { name: '保存执行设置', exact: true }).click();
  await expect(form.getByRole('status')).toContainText('本次保存已确认');
  expect(state.saved['app.agent_concurrency']).toBe(4);
  expect(state.requests[0].body.values).toEqual({ 'product.max_tool_calls': 180 });
  expect(state.requests[0].body.expected_configuration_revision).toBe('sha256:' + '2'.padStart(64, '0'));
});

test('lost save acknowledgement reuses its operation and does not replace a later file edit with the old receipt', async ({ page, server }) => {
  const { panel, form, state } = await setup(page, server);
  state.abortFirst = true;
  const tools = form.getByLabel('每工作工具调用上限（次）', { exact: true });
  await tools.fill('250'); await form.getByRole('button', { name: '保存执行设置', exact: true }).click();
  await expect(form.getByRole('button', { name: '核对原保存', exact: true })).toBeEnabled();
  await expect(tools).toBeDisabled();
  state.external('product.max_tool_calls', 275);
  await panel.getByRole('button', { name: '重新读取执行设置', exact: true }).click();
  await expect(form).toContainText('文件值 275'); await expect(tools).toHaveValue('250');
  await form.getByRole('button', { name: '核对原保存', exact: true }).click();
  await expect(tools).toHaveValue('275'); await expect(tools).toBeEnabled();
  expect(state.requests).toHaveLength(2); expect(state.requests[0]).toEqual(state.requests[1]);
  expect(state.applied).toBe(1); expect(state.saved['product.max_tool_calls']).toBe(275);
  expect(state.forbidden).toEqual([]);
});

test('a file conflict keeps the draft until the owner adopts the latest file version', async ({ page, server }) => {
  const { form, state } = await setup(page, server);
  state.conflictNext = true;
  const tools = form.getByLabel('每工作工具调用上限（次）', { exact: true });
  await tools.fill('180'); await form.getByRole('button', { name: '保存执行设置', exact: true }).click();
  await expect(form.getByRole('alert')).toContainText('文件已被其他编辑修改');
  await expect(tools).toHaveValue('180'); await expect(tools).toBeEnabled();
  await expect(form.getByRole('button', { name: '保存执行设置', exact: true })).toBeDisabled();
  await form.getByRole('button', { name: '采用当前文件版本并保留我的修改', exact: true }).click();
  await form.getByRole('button', { name: '保存执行设置', exact: true }).click();
  await expect(form.getByRole('status')).toContainText('本次保存已确认');
  expect(state.applied).toBe(1); expect(state.saved['app.node_active_seconds']).toBe(900);
  expect(state.requests[0].key).not.toBe(state.requests[1].key);
});

test('execution range checks and confirmed validation errors keep the form editable', async ({ page, server }) => {
  const { form, state } = await setup(page, server);
  const time = form.getByLabel('每工作累计执行时长（秒）', { exact: true });
  await time.fill('0'); await expect(form.getByRole('button', { name: '保存执行设置', exact: true })).toBeDisabled();
  await time.fill('86401'); await expect(form.getByRole('button', { name: '保存执行设置', exact: true })).toBeDisabled();
  expect(state.requests).toHaveLength(0);
  await time.fill('2400'); state.rejectNext = true;
  await form.getByRole('button', { name: '保存执行设置', exact: true }).click();
  await expect(form.getByRole('alert')).toContainText('请修正执行设置输入');
  await expect(time).toBeEnabled();
  await time.fill('2500'); await form.getByRole('button', { name: '保存执行设置', exact: true }).click();
  await expect(form.getByRole('status')).toContainText('本次保存已确认');
  expect(state.applied).toBe(1); expect(state.requests[0].key).not.toBe(state.requests[1].key);
});

test('unconfirmed file save remains frozen until explicitly accepting the current file without another write', async ({ page, server }) => {
  const { form, state } = await setup(page, server);
  const tools = form.getByLabel('每工作工具调用上限（次）', { exact: true });
  await tools.fill('180'); state.unconfirmed = true;
  await form.getByRole('button', { name: '保存执行设置', exact: true }).click();
  await expect(form.getByRole('button', { name: '核对原保存', exact: true })).toBeEnabled();
  await expect(tools).toBeDisabled();
  await form.getByRole('button', { name: '核对原保存', exact: true }).click();
  expect(state.requests[0]).toEqual(state.requests[1]);
  await form.getByRole('button', { name: '以当前文件为准', exact: true }).click();
  await expect(tools).toHaveValue('100'); await expect(tools).toBeEnabled();
  await expect(form.getByRole('status')).toContainText('没有发起新的保存');
  expect(state.requests).toHaveLength(2); expect(state.applied).toBe(0);
});

for (const status of [409, 422]) test(`file durability failure ${status} retains the original operation after replacement`, async ({ page, server }) => {
  const { form, state } = await setup(page, server);
  const tools = form.getByLabel('每工作工具调用上限（次）', { exact: true });
  await tools.fill('180'); state.durabilityFailureStatus = status;
  await form.getByRole('button', { name: '保存执行设置', exact: true }).click();
  await expect(form.getByRole('alert')).toContainText('文件持久化尚未确认');
  await expect(tools).toBeDisabled();
  await expect(form.getByRole('status')).toHaveCount(0);
  await form.getByRole('button', { name: '核对原保存', exact: true }).click();
  await expect(form.getByRole('status')).toContainText('本次保存已确认');
  await expect(tools).toHaveValue('180'); await expect(tools).toBeEnabled();
  expect(state.requests).toHaveLength(2); expect(state.requests[0]).toEqual(state.requests[1]);
  expect(state.applied).toBe(1);
});


test('public research switch saves a boolean and preserves current task authorizations', async ({ page, server }) => {
  const { panel, form, state } = await setup(page, server);
  await form.getByText('高级执行策略', { exact: true }).click();
  const toggle = form.getByRole('combobox', { name: '公开网页调研（开关）', exact: true });
  await expect(toggle).toHaveValue('true');
  await toggle.selectOption('false');
  await form.getByRole('button', { name: '保存执行设置', exact: true }).click();
  await expect(panel).toContainText('文件值 关闭；本次已加载 开启；默认 开启');
  expect(state.requests[0].body.values).toEqual({ 'app.research_public_web_enabled': false });
  expect(state.applied).toBe(1);
  expect(state.forbidden).toEqual([]);
});


test('test repair policy saves continuous and disabled modes without changing model or run limits', async ({ page, server }) => {
  const { form, state } = await setup(page, server);
  await form.getByText('高级执行策略', { exact: true }).click();
  const input = form.getByLabel('测试失败自动修复次数（次）', { exact: true });
  const field = form.locator('.execution-setting-field').filter({ hasText: 'app.auto_test_repair_limit' });
  const save = form.getByRole('button', { name: '保存执行设置', exact: true });
  await expect(input).toHaveValue('100');
  await input.fill('-2'); await expect(save).toBeDisabled();
  await input.fill('-1'); await expect(save).toBeEnabled();
  await save.click();
  await expect(field).toContainText('文件值 持续修复；本次已加载 100；默认 100 次');
  expect(state.requests[0].body.values).toEqual({ 'app.auto_test_repair_limit': -1 });
  await input.fill('0'); await expect(save).toBeEnabled();
  await save.click();
  await expect(field).toContainText('文件值 0；本次已加载 100；默认 100 次');
  expect(state.requests[1].body.values).toEqual({ 'app.auto_test_repair_limit': 0 });
  expect(state.forbidden).toEqual([]);
});

test('retry policies default to one hundred and accept that value for an existing lower configuration', async ({ page, server }) => {
  const keys = ['app.auto_failure_retry_limit', 'app.auto_failure_run_limit', 'app.auto_timeout_retry_limit',
    'app.auto_review_repair_limit', 'app.auto_test_repair_limit'];
  const { form, state } = await setup(page, server, Object.fromEntries(keys.map(key => [key, 2])));
  await form.getByText('高级执行策略', { exact: true }).click();
  for (const key of keys) {
    const field = form.locator('.execution-setting-field').filter({ has: page.locator('code', { hasText: key }) });
    await expect(field).toContainText('默认 100');
    const input = field.locator('input');
    await expect(input).toHaveValue('2');
    await input.fill('100');
  }
  const save = form.getByRole('button', { name: '保存执行设置', exact: true });
  await expect(save).toBeEnabled(); await save.click();
  await expect(form.getByRole('status')).toContainText('本次保存已确认');
  expect(state.requests[0].body.values).toEqual(Object.fromEntries(keys.map(key => [key, 100])));
  expect(state.forbidden).toEqual([]);
});
