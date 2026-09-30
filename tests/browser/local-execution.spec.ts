import type * as Playwright from '../../apps/dashboard/node_modules/@playwright/test/index.js';
import { createRequire } from 'node:module';
import { spawn } from 'node:child_process';
import { mkdtemp, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import path from 'node:path';

const { test: base, expect } = createRequire(import.meta.url)('../../apps/dashboard/node_modules/@playwright/test/index.js') as typeof Playwright;
type Server = { origin: string; bootstrap: string; fixture_key: string; run: string; work: string };
const repository = path.resolve(import.meta.dirname, '../..');
const test = base.extend<{ server: Server }>({
  server: async ({ page }, use) => {
    const directory = await mkdtemp(path.join(tmpdir(), 'agentflow-local-wait-ui-'));
    const child = spawn(path.join(repository, '.venv/bin/python'), ['-u', 'tests/browser/server.py', '--directory', directory], {
      cwd: repository, env: { ...process.env, PYTHONPATH: path.join(repository, 'src') }, stdio: ['ignore', 'pipe', 'pipe'],
    });
    let errors = ''; child.stderr.on('data', chunk => { errors += String(chunk); });
    try {
      const server = await new Promise<Server>((resolve, reject) => {
        let output = ''; const timeout = setTimeout(() => reject(new Error(errors || 'Local wait fixture startup timed out')), 20000);
        child.once('exit', code => { clearTimeout(timeout); reject(new Error(`Fixture exited ${code}: ${errors}`)); });
        child.stdout.on('data', chunk => {
          output += String(chunk); const line = output.split('\n').find(value => value.startsWith('{"origin"'));
          if (line) { clearTimeout(timeout); resolve(JSON.parse(line)); }
        });
      });
      await expect.poll(async () => { try { return (await fetch(server.origin + '/health')).status; } catch { return 0; } }).toBe(200);
      const graph = await fixture(server, 'full_workflow', {});
      server.work = graph.stage_ids.unit_test_execution;
      await fixture(server, 'workflow_state', { step: 'unit_test_execution', status: 'waiting_execution' });
      await use(server);
    } finally {
      await page.goto('about:blank').catch(() => undefined); child.kill('SIGTERM');
      await new Promise<void>(resolve => { if (child.exitCode !== null) resolve(); else {
        const timeout = setTimeout(() => { child.kill('SIGKILL'); resolve(); }, 5000);
        child.once('exit', () => { clearTimeout(timeout); resolve(); });
      } });
      await rm(directory, { recursive: true, force: true });
    }
  },
});

async function fixture(server: Server, endpoint: string, payload: unknown) {
  const response = await fetch(server.origin + '/__fixture/' + endpoint, {
    method: 'POST', headers: { 'X-Fixture-Key': server.fixture_key, Origin: server.origin,
      'Idempotency-Key': crypto.randomUUID(), 'Content-Type': 'application/json' }, body: JSON.stringify(payload),
  });
  expect(response.ok).toBeTruthy(); return response.json();
}

const localTarget = { target_config_id: 'managed-local-api', app_target: 'api', revision: 1,
  os_name: 'Darwin', os_version_constraint: '*', cpu_architecture: 'arm64',
  ui_framework: null, ui_framework_version_constraint: null, required_display_protocol: 'not_required',
  required_device_mode: 'not_required', device_model_constraints: [], required_resource_ids: ['managed-workspace'],
  sdk_requirements: [{ name: 'node', version_constraint: '>=22.13' }, { name: 'npm', version_constraint: '>=10' }],
  build_backend: { name: 'npm', version_constraint: '>=10' }, test_backend: { name: 'node', version_constraint: '>=22.13' },
  required_capabilities: [], source_test_plan_ref: null };
const blocked = { state: 'blocked', phase: 'blocked', preparing: false, error_code: 'local_reference_failed',
  message: '本机验证未通过：build。tool_execution_failed: timeout',
  detail: '本机验证未通过：build。tool_execution_failed: timeout', target_configs: [localTarget], ready_targets: [] };

async function arrange(page: Playwright.Page, server: Server, overrides: Record<string, unknown> = {}) {
  const state = { local: { ...blocked, ...overrides }, jobs: [{ id: 'queued-local-build', revision: 1,
    run_id: server.run, parent_work_item_id: server.work, parent_generation: 1, state: 'queued',
    app_target: 'api', target_config: localTarget, quality_result: 'unknown', kind: 'build', node_id: null }] };
  // Only the external executor status is controlled here. Run/workflow state,
  // authentication and rendering use the real owner service and stored graph.
  await page.route('**/api/v1/product_setup', route => route.fulfill({ json: {
    ready: false, models_ready: false, requirements: [], model_bindings: {}, profiles: [], local_execution: state.local,
  } }));
  await page.route('**/api/v1/executor_jobs', route => route.fulfill({ json: { items: state.jobs } }));
  return state;
}

async function enter(page: Playwright.Page, server: Server) {
  await page.goto(`${server.origin}/#bootstrap=${encodeURIComponent(server.bootstrap)}`);
  await expect(page.getByRole('heading', { name: '创建产品', exact: true })).toBeVisible();
  await page.getByRole('button', { name: '执行台', exact: true }).click();
  await expect(page.getByRole('button', { name: /^13\. 单元测试，/ })).toContainText('等待');
}

test('a queued local task exposes the validation failure and an explicit preparation action', async ({ page, server }) => {
  await arrange(page, server);
  const posts: string[] = [];
  page.on('request', request => { if (request.method() === 'POST') posts.push(new URL(request.url()).pathname); });
  await enter(page, server);
  const notice = page.getByRole('alert').filter({ hasText: '本机执行器准备失败' });
  await expect(notice).toBeVisible();
  await expect(notice).toContainText('构建');
  await expect(notice).toContainText('timeout');
  await expect(notice.getByRole('button', { name: '重新准备本机执行器', exact: true })).toBeEnabled();
  expect(posts.filter(url => url !== '/api/v1/session')).toEqual([]);
  await page.screenshot({ path: path.join(repository, 'validation/local-execution-wait-status.png'), fullPage: true });
});

test('preparation is single-flight, then follows the actual preparation and ready states', async ({ page, server }) => {
  const state = await arrange(page, server);
  const requests: { path: string; key?: string; body: unknown }[] = [];
  let release!: () => void; const gate = new Promise<void>(resolve => { release = resolve; });
  await page.route('**/api/v1/product_setup/local_execution', async route => {
    requests.push({ path: new URL(route.request().url()).pathname, key: route.request().headers()['idempotency-key'], body: route.request().postDataJSON() });
    await gate;
    state.local = { ...state.local, state: 'preparing', phase: 'build', preparing: true, error_code: '',
      message: '正在真实构建验证样本', detail: '正在真实构建验证样本' };
    await route.fulfill({ json: state.local });
  });
  await enter(page, server);
  const button = page.getByRole('button', { name: '重新准备本机执行器', exact: true });
  try {
    await button.evaluate((element: HTMLButtonElement) => { element.click(); element.click(); });
    await expect.poll(() => requests.length).toBe(1);
    await expect(page.getByRole('button', { name: '正在提交…', exact: true })).toBeDisabled();
  } finally { release(); }
  await expect(page.getByRole('status').filter({ hasText: '本机执行器准备中' })).toContainText('正在真实构建验证样本');
  await expect(button).toHaveCount(0);
  state.local = { ...state.local, state: 'ready', phase: 'ready', preparing: false, message: '所需本机执行环境已通过真实验证', detail: '所需本机执行环境已通过真实验证' };
  await expect(page.getByRole('status').filter({ hasText: '本机执行器已就绪' })).toBeVisible();
  await expect(button).toHaveCount(0);
  expect(requests).toHaveLength(1);
  expect(requests[0]).toMatchObject({ path: '/api/v1/product_setup/local_execution', body: {} });
  expect(requests[0].key).toBeTruthy();
});

test('a lost preparation acknowledgement retains the same idempotency key on explicit retry', async ({ page, server }) => {
  await arrange(page, server);
  const requests: { key?: string; body: unknown }[] = [];
  await page.route('**/api/v1/product_setup/local_execution', async route => {
    requests.push({ key: route.request().headers()['idempotency-key'], body: route.request().postDataJSON() });
    await route.abort();
  });
  await enter(page, server);
  const button = page.getByRole('button', { name: '重新准备本机执行器', exact: true });
  await button.click();
  await expect(page.getByRole('alert').filter({ hasText: '操作结果尚未确认' })).toBeVisible();
  await button.click();
  await expect.poll(() => requests.length).toBe(2);
  expect(requests[0].key).toBeTruthy();
  expect(requests[1]).toEqual(requests[0]);
});

test('a stopped local executor shows its actual reconnect reason', async ({ page, server }) => {
  await arrange(page, server, { state: 'unprepared', phase: 'reconnect', error_code: null,
    message: '启动本机执行器后即可复用已验证环境', detail: '启动本机执行器后即可复用已验证环境' });
  await enter(page, server);
  const notice = page.getByRole('status').filter({ hasText: '本机执行器尚未启动' });
  await expect(notice).toContainText('启动本机执行器后即可复用已验证环境');
  await expect(notice.getByRole('button', { name: '准备本机执行器', exact: true })).toBeEnabled();
});

test('an active preparation suppresses stale failure actions', async ({ page, server }) => {
  await arrange(page, server, { preparing: true });
  await enter(page, server);
  await expect(page.getByRole('status').filter({ hasText: '本机执行器准备中' })).toBeVisible();
  await expect(page.getByRole('button', { name: '重新准备本机执行器', exact: true })).toHaveCount(0);
});

test('a ready local target does not inherit the failure of another local platform', async ({ page, server }) => {
  await arrange(page, server, { state: 'partial', ready_targets: ['api'] });
  await enter(page, server);
  await expect(page.getByRole('status').filter({ hasText: '本机执行器已就绪' })).toBeVisible();
  await expect(page.getByRole('button', { name: '重新准备本机执行器', exact: true })).toHaveCount(0);
});

for (const mismatch of ['revision', 'os_name', 'required_resource_ids', 'sdk_order', 'backend_version'] as const) {
  test(`a shared target ID with a different complete configuration is not local: ${mismatch}`, async ({ page, server }) => {
    const state = await arrange(page, server, { state: 'ready', ready_targets: ['api'], error_code: null,
      message: '所需本机执行环境已通过真实验证', detail: '所需本机执行环境已通过真实验证' });
    state.jobs[0].target_config = { ...localTarget };
    if (mismatch === 'revision') state.jobs[0].target_config.revision = 2;
    if (mismatch === 'os_name') state.jobs[0].target_config.os_name = 'Linux';
    if (mismatch === 'required_resource_ids') state.jobs[0].target_config.required_resource_ids = ['remote-workspace'];
    if (mismatch === 'sdk_order') state.jobs[0].target_config.sdk_requirements = [...localTarget.sdk_requirements].reverse();
    if (mismatch === 'backend_version') state.jobs[0].target_config.build_backend = { name: 'npm', version_constraint: '>=11' };
    const jobsRead = page.waitForResponse('**/api/v1/executor_jobs');
    await enter(page, server); await jobsRead;
    await expect(page.getByRole('status').filter({ hasText: '本机执行器已就绪' })).toHaveCount(0);
    await expect(page.getByRole('alert').filter({ hasText: '本机执行器准备失败' })).toHaveCount(0);
    await expect(page.getByRole('button', { name: '重新准备本机执行器', exact: true })).toHaveCount(0);
  });
}

test('equivalent complete target configurations match despite object key order', async ({ page, server }) => {
  const state = await arrange(page, server);
  state.jobs[0].target_config = { ...Object.fromEntries(Object.entries(localTarget).reverse()),
    build_backend: { version_constraint: '>=10', name: 'npm' },
    sdk_requirements: [{ version_constraint: '>=22.13', name: 'node' }, { version_constraint: '>=10', name: 'npm' }],
  } as typeof localTarget;
  await enter(page, server);
  await expect(page.getByRole('alert').filter({ hasText: '本机执行器准备失败' })).toBeVisible();
  await expect(page.getByRole('button', { name: '重新准备本机执行器', exact: true })).toBeEnabled();
});

for (const mismatch of ['target', 'run', 'work', 'generation', 'job_state', 'work_state'] as const) {
  test(`local failure is not attributed to a task with a different ${mismatch}`, async ({ page, server }) => {
    const state = await arrange(page, server);
    if (mismatch === 'target') state.jobs[0].target_config = { ...localTarget, target_config_id: 'remote-api-same-platform' };
    if (mismatch === 'run') state.jobs[0].run_id = 'another-run';
    if (mismatch === 'work') state.jobs[0].parent_work_item_id = 'another-work-item';
    if (mismatch === 'generation') state.jobs[0].parent_generation = 0;
    if (mismatch === 'job_state') state.jobs[0].state = 'completed';
    const jobsRead = page.waitForResponse('**/api/v1/executor_jobs');
    await enter(page, server);
    await jobsRead;
    if (mismatch === 'work_state') {
      await expect(page.getByRole('alert').filter({ hasText: '本机执行器准备失败' })).toBeVisible();
      await fixture(server, 'workflow_state', { step: 'unit_test_execution', status: 'running' });
      await page.getByRole('button', { name: '刷新数据', exact: true }).click();
      await expect(page.getByRole('button', { name: /^13\. 单元测试，/ })).toContainText('执行中');
    }
    await expect(page.getByRole('alert').filter({ hasText: '本机执行器准备失败' })).toHaveCount(0);
    await expect(page.getByRole('button', { name: '重新准备本机执行器', exact: true })).toHaveCount(0);
  });
}
