import type * as Playwright from '../../apps/dashboard/node_modules/@playwright/test/index.js';
import { createRequire } from 'node:module';
import { spawn } from 'node:child_process';
import { mkdtemp, readFile, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import path from 'node:path';

const { test: base, expect } = createRequire(import.meta.url)('../../apps/dashboard/node_modules/@playwright/test/index.js') as typeof Playwright;
type Server = { origin: string; bootstrap: string; fixture_key: string; directory: string;
  source_project?: string; multi_project?: string; unknown_project?: string };
const repository = path.resolve(import.meta.dirname, '../..');
const test = base.extend<{ server: Server; production: boolean }>({
  production: [false, { option: true }],
  server: async ({ page, production }, use) => {
    const directory = await mkdtemp(path.join(tmpdir(), 'agentflow-product-ui-'));
    const child = spawn(path.join(repository, '.venv/bin/python'), ['-u', production ? 'tests/browser/product_entry_server.py' : 'tests/browser/product_server.py', '--directory', directory], {
      cwd: repository, env: { ...process.env, PYTHONPATH: path.join(repository, 'src') }, stdio: ['ignore', 'pipe', 'pipe'],
    });
    let errors = ''; child.stderr.on('data', data => { errors += String(data); });
    try {
      const server = await new Promise<Server>((resolve, reject) => {
        let buffer = ''; const timeout = setTimeout(() => reject(new Error(errors || 'Product fixture startup timed out')), 20000);
        child.once('exit', code => { clearTimeout(timeout); reject(new Error(`Product fixture exited ${code}: ${errors}`)); });
        child.stdout.on('data', data => {
          buffer += String(data); const line = buffer.split('\n').find(value => value.startsWith('{"origin"'));
          if (line) { clearTimeout(timeout); resolve(JSON.parse(line)); }
        });
      });
      await expect.poll(async () => { try { return (await fetch(server.origin + '/health')).status; } catch { return 0; } }).toBe(200);
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

async function fixture(server: Server, endpoint: string, value?: unknown) {
  const response = await fetch(server.origin + '/__fixture/' + endpoint, {
    method: value === undefined ? 'GET' : 'POST', headers: { 'X-Fixture-Key': server.fixture_key,
      Origin: server.origin, 'Idempotency-Key': crypto.randomUUID(), 'Content-Type': 'application/json' },
    ...(value === undefined ? {} : { body: JSON.stringify(value) }),
  });
  expect(response.ok).toBeTruthy(); return response.json();
}

async function enter(page: Playwright.Page, server: Server, ready = true) {
  if (ready) await fixture(server, 'setup', { roles: true, coding: true, local_state: 'ready' });
  await page.goto(`${server.origin}/#bootstrap=${encodeURIComponent(server.bootstrap)}`);
  await expect(page.getByRole('heading', { name: '创建产品', exact: true })).toBeVisible();
}

async function fillProduct(page: Playwright.Page, name = '浏览器契约产品') {
  const form = page.getByRole('form', { name: '创建产品', exact: true });
  await form.getByLabel('产品名称', { exact: true }).fill(name);
  await form.getByLabel('产品目标', { exact: true }).fill('为我的团队实现可新增、查询和归档事项的产品，并通过审查与测试。');
  return form;
}

async function createProduct(page: Playwright.Page, server: Server) {
  const form = await fillProduct(page);
  await form.getByRole('button', { name: '开始创建产品' }).click();
  await expect(page.getByRole('heading', { name: '我的产品', exact: true })).toBeVisible();
  await expect(page.getByText('正在准备研发环境与执行计划', { exact: true })).toBeVisible();
  return (await fixture(server, 'state')).products[0];
}

test('platform choices wait for defaults and preserve an explicit Web/API combination', async ({ page, server }) => {
  await fixture(server, 'setup', { roles: true, coding: true, local_state: 'ready',
    product_defaults: { target: 'api', review_mode: 'auto', max_model_requests: 200 } });
  let release!: () => void;
  const gate = new Promise<void>(resolve => { release = resolve; });
  await page.route('**/api/v1/product_setup', async route => {
    const response = await route.fetch();
    await gate;
    await route.fulfill({ response });
  });
  try {
    await page.goto(`${server.origin}/#bootstrap=${encodeURIComponent(server.bootstrap)}`);
    const form = page.getByRole('form', { name: '创建产品', exact: true });
    const web = form.getByRole('checkbox', { name: 'Web 应用', exact: true });
    const api = form.getByRole('checkbox', { name: 'API 服务', exact: true });
    await expect(web).toBeDisabled();
    await expect(web).not.toBeChecked();
    release();
    await expect(api).toBeChecked();
    await expect(web).toBeEnabled();
    await web.check();
    await fillProduct(page);
    await form.getByRole('button', { name: '开始创建产品', exact: true }).click();
    await expect.poll(async () => (await fixture(server, 'state')).products.length).toBe(1);
    expect((await fixture(server, 'state')).products[0].targets).toEqual(['web', 'api']);
  } finally { release(); }
});

test('new product is the main entrance and model credentials are cleared after explicit setup', async ({ page, server }) => {
  await enter(page, server, false);
  const form = await fillProduct(page);
  await expect(form.getByLabel('人工参与方式')).toHaveValue('auto');
  await expect(form.getByRole('button', { name: '开始创建产品' })).toBeDisabled();
  await expect(page.locator('.my-products-list-panel')).not.toBeVisible();
  await page.getByRole('navigation', { name: '主导航' }).getByRole('button', { name: '我的产品', exact: true }).click();
  await expect(page.getByText('还没有产品', { exact: true })).toBeVisible();
  await page.getByRole('navigation', { name: '主导航' }).getByRole('button', { name: '创建产品', exact: true }).click();
  await page.getByRole('button', { name: '设置模型', exact: true }).click();
  const setup = page.getByRole('form', { name: '设置研发模型' });
  await setup.getByLabel('模型用途').selectOption('both');
  await setup.getByLabel('模型名称', { exact: true }).fill('browser-contract-model');
  await setup.getByLabel('API Key', { exact: true }).fill('fake-ui-only-secret');
  const sent = page.waitForRequest(request => request.url().endsWith('/api/v1/product_setup/models') && request.method() === 'POST');
  await setup.getByRole('button', { name: '保存并使用此模型' }).click();
  expect((await sent).postDataJSON()).toMatchObject({ role: 'both', api_key: 'fake-ui-only-secret', model: 'browser-contract-model' });
  await expect(setup.getByLabel('API Key', { exact: true })).toHaveValue('');
  await expect(page.locator('body')).not.toContainText('fake-ui-only-secret');
  await expect(form.getByRole('button', { name: '开始创建产品' })).toBeEnabled();
  expect(await page.evaluate(() => [localStorage.length, sessionStorage.length, document.cookie])).toEqual([0, 1, '']);
  expect((await fixture(server, 'state')).model_requests[0]).toMatchObject({ role: 'both', key_provided: true });
  expect(JSON.stringify(await fixture(server, 'state'))).not.toContain('fake-ui-only-secret');
});

test('one product request carries the goal and follows the returned Run without client-side plan orchestration', async ({ page, server }) => {
  await enter(page, server);
  const requests: string[] = []; page.on('request', request => { if (request.method() === 'POST') requests.push(new URL(request.url()).pathname); });
  const form = await fillProduct(page, '事项 API');
  await form.getByLabel('Web 应用', { exact: true }).uncheck();
  await form.getByLabel('API 服务').check();
  await form.getByLabel('输出目录（可选）').fill(path.join(server.directory, 'api-output'));
  await form.getByLabel('人工参与方式').selectOption('every_step');
  await form.getByRole('button', { name: '开始创建产品' }).click();
  await expect(page.getByText('正在准备研发环境与执行计划', { exact: true })).toBeVisible();
  const observed = await fixture(server, 'state'); const product = observed.products[0];
  expect(observed.product_requests[0].payload).toMatchObject({ name: '事项 API', targets: ['api'], creation_mode: 'new', review_mode: 'every_step', output_directory: path.join(server.directory, 'api-output') });
  expect(requests).toEqual(['/api/v1/products']);
  await fixture(server, 'product', { id: product.id, state: 'running' });
  await expect(page.getByRole('button', { name: '查看 Agent 和研发进展', exact: true })).toBeVisible();
  await page.getByRole('button', { name: '查看 Agent 和研发进展', exact: true }).click();
  await expect(page.getByRole('heading', { name: '执行台', exact: true }).first()).toBeVisible();
  await expect(page.getByRole('combobox', { name: '需求变更', exact: true })).toHaveValue('initial');
});

test('product progress counts a completed review only after its verdict passes', async ({ page, server }) => {
  await enter(page, server); const product = await createProduct(page, server);
  await fixture(server, 'product', { id: product.id, state: 'running', review_quality: 'failed' });
  await expect(page.locator('.product-progress-line')).toContainText('已完成 0 / 2 项工作');
  await fixture(server, 'product', { id: product.id, state: 'running', review_quality: 'unknown' });
  await expect(page.locator('.product-progress-line')).toContainText('已完成 0 / 2 项工作');
  await fixture(server, 'product', { id: product.id, state: 'running', review_quality: 'passed' });
  await expect(page.locator('.product-progress-line')).toContainText('已完成 1 / 2 项工作');
});

test('completion alone never unlocks download; a complete delivery downloads through owner authentication', async ({ page, server }) => {
  await enter(page, server); const product = await createProduct(page, server);
  await fixture(server, 'product', { id: product.id, state: 'completed' });
  await expect(page.getByText('运行已结束，交付信息尚未齐备', { exact: true })).toBeVisible();
  await expect(page.getByRole('button', { name: '下载产品源码' })).toHaveCount(0);
  await fixture(server, 'product', { id: product.id, state: 'completed', delivery: true });
  await expect(page.getByTestId('product-delivery')).toBeVisible();
  const downloaded = page.waitForEvent('download');
  const request = page.waitForRequest(value => value.url().endsWith(`/products/${product.id}/download`));
  await page.getByRole('button', { name: '下载产品源码', exact: true }).click();
  const download = await downloaded;
  expect(download.suggestedFilename()).toBe('product-source.zip');
  const saved = await download.path(); expect((await readFile(saved!)).subarray(0, 2).toString()).toBe('PK');
  expect((await request).headers().authorization).toMatch(/^Bearer /);
  expect((await request).url()).not.toContain('Bearer');
  await page.getByText('查看启动说明', { exact: true }).click();
  await expect(page.locator('.launch-instructions')).toContainText('node server.js');
  await page.getByRole('button', { name: '启动产品', exact: true }).click();
  await expect(page.getByRole('link', { name: '打开产品', exact: true })).toHaveAttribute('href', 'http://127.0.0.1:18888/');
  await page.getByRole('button', { name: '停止产品', exact: true }).click();
  await expect(page.getByRole('link', { name: '打开产品', exact: true })).toHaveCount(0);
  await expect(page.getByRole('button', { name: '启动产品', exact: true })).toBeVisible();
});

test('a lost create acknowledgement retries the same intent and does not create a second product', async ({ page, server }) => {
  await enter(page, server); const form = await fillProduct(page);
  let first = true;
  await page.route('**/api/v1/products', async route => {
    if (route.request().method() === 'POST' && first) { first = false; await route.fetch(); await route.abort(); }
    else await route.continue();
  });
  await form.getByRole('button', { name: '开始创建产品' }).click();
  await expect(form.getByRole('alert')).toContainText('操作结果尚未确认');
  await form.getByRole('button', { name: '开始创建产品' }).click();
  await expect(page.getByText('正在准备研发环境与执行计划', { exact: true })).toBeVisible();
  const observed = await fixture(server, 'state');
  expect(observed.products).toHaveLength(1); expect(observed.product_requests).toHaveLength(2);
  expect(observed.product_requests[0].key).toBe(observed.product_requests[1].key);
});

test('cross-origin downloads and non-local launch URLs never receive owner credentials or actionable links', async ({ page, server }) => {
  await enter(page, server); const product = await createProduct(page, server);
  await fixture(server, 'product', { id: product.id, state: 'completed', delivery: true, download_url: 'https://invalid.example/source.zip' });
  await fixture(server, 'setup', { launch_url: 'https://invalid.example/product' });
  const external: string[] = []; page.on('request', request => { if (new URL(request.url()).hostname === 'invalid.example') external.push(request.url()); });
  await expect(page.getByTestId('product-delivery')).toBeVisible();
  await page.getByRole('button', { name: '下载产品源码', exact: true }).click();
  await expect(page.getByRole('alert')).toContainText('拒绝非管理接口地址');
  await page.getByRole('button', { name: '启动产品', exact: true }).click();
  await expect(page.getByRole('alert')).toContainText('不是本机地址');
  await expect(page.getByRole('link', { name: '打开产品' })).toHaveCount(0);
  expect(external).toEqual([]);
});

test('blocked prerequisites remain visible until an actual setup response changes them', async ({ page, server }) => {
  await enter(page, server); await fixture(server, 'setup', { local_state: 'blocked', local_detail: '本机执行隔离检查尚未通过' });
  const form = await fillProduct(page);
  await expect(page.getByText('本机执行隔离检查尚未通过', { exact: true })).toBeVisible();
  await expect(form.getByRole('button', { name: '开始创建产品' })).toBeDisabled();
  await page.getByRole('button', { name: '重新准备本机环境', exact: true }).click();
  await expect(page.getByRole('button', { name: '本机环境准备中…', exact: true })).toBeDisabled();
  await expect(page.getByText('研发条件已齐备', { exact: false })).toHaveCount(0);
  expect((await fixture(server, 'state')).prepare_requests).toBe(1);
});

test('only an unstarted preparation failure offers retry and a lost request reuses the intent', async ({ page, server }) => {
  await enter(page, server); const product = await createProduct(page, server);
  await fixture(server, 'product', { id: product.id, state: 'blocked', blocking_reasons: ['准备中断，请重试'] });
  const retry = page.getByRole('button', { name: '重试准备', exact: true });
  await expect(retry).toBeVisible();
  let first = true; const keys: string[] = [];
  await page.route(`**/api/v1/products/${product.id}/retry`, async route => {
    keys.push(route.request().headers()['idempotency-key']);
    if (first) { first = false; await route.abort(); } else await route.continue();
  });
  await retry.click();
  await expect(page.getByRole('alert')).toContainText('操作结果尚未确认');
  await retry.click();
  await expect(page.getByText('正在准备研发环境与执行计划', { exact: true })).toBeVisible();
  expect(keys).toHaveLength(2); expect(keys[0]).toBe(keys[1]);
  expect((await fixture(server, 'state')).products).toHaveLength(1);
  await fixture(server, 'product', { id: product.id, state: 'blocked', restore_reconciliation_required: true });
  await expect(page.getByText('研发已暂停，等待处理以下问题', { exact: true })).toBeVisible();
  await expect(retry).toHaveCount(0);
  await fixture(server, 'product', { id: product.id, state: 'running', restore_reconciliation_required: false });
  await expect(page.getByRole('button', { name: '查看 Agent 和研发进展', exact: true })).toBeVisible();
  // Hold an actual older detail response while the independent list poll
  // receives a newer revision. No manual UI refresh or timeout increase.
  let releaseDetail!: () => void; let detailCaptured!: () => void; let staleDelivered!: () => void;
  const heldDetail = new Promise<void>(resolve => { releaseDetail = resolve; });
  const captured = new Promise<void>(resolve => { detailCaptured = resolve; });
  const delivered = new Promise<void>(resolve => { staleDelivered = resolve; });
  let holdFirst = true;
  await page.route(`**/api/v1/products/${product.id}`, async route => {
    if (route.request().method() === 'GET' && holdFirst) {
      holdFirst = false;
      const response = await route.fetch();
      expect((await response.json()).state).toBe('running');
      detailCaptured();
      await heldDetail;
      await route.fulfill({ response });
      staleDelivered();
    } else await route.continue();
  });
  try {
    await captured;
    const collectionPoll = page.waitForRequest(request => request.method() === 'GET'
      && new URL(request.url()).pathname === '/api/v1/products', { timeout: 5000 });
    const currentList = page.waitForResponse(async response => {
      if (new URL(response.url()).pathname !== '/api/v1/products' || response.request().method() !== 'GET' || response.status() !== 200) return false;
      const current = (await response.json()).items.find((item: { id: string }) => item.id === product.id);
      return current?.state === 'blocked' && current.run_id && !current.restore_reconciliation_required;
    }, { timeout: 10000 });
    const changed = await fixture(server, 'product', { id: product.id, state: 'blocked' });
    // Assert that polling still happens within its normal cadence, separately
    // from transport completion under host I/O load. Rendering keeps the usual
    // 5-second expectation after the exact server revision has arrived.
    await collectionPoll;
    const listed = (await (await currentList).json()).items.find((item: { id: string }) => item.id === product.id);
    expect(listed.revision).toBe(changed.revision);
    await expect(page.getByText('研发已暂停，等待处理以下问题', { exact: true })).toBeVisible();
    await expect(retry).toHaveCount(0);
    releaseDetail();
    await delivered;
    await page.evaluate(() => new Promise<void>(resolve => requestAnimationFrame(() => requestAnimationFrame(() => resolve()))));
    await expect(page.getByText('研发已暂停，等待处理以下问题', { exact: true })).toBeVisible();
    await expect(retry).toHaveCount(0);
  } finally { releaseDetail(); }
});

test('polled launch state removes a stale product link and prevents restart while execution is unknown', async ({ page, server }) => {
  await enter(page, server); const product = await createProduct(page, server);
  await fixture(server, 'product', { id: product.id, state: 'completed', delivery: true });
  await page.getByRole('button', { name: '启动产品', exact: true }).click();
  await expect(page.getByRole('link', { name: '打开产品', exact: true })).toBeVisible();
  await fixture(server, 'product', { id: product.id, state: 'completed', launch: { state: 'stopped', url: null } });
  await expect(page.getByRole('link', { name: '打开产品', exact: true })).toHaveCount(0);
  await expect(page.getByRole('button', { name: '启动产品', exact: true })).toBeVisible();
  await fixture(server, 'product', { id: product.id, state: 'completed', launch: { state: 'execution_unknown', url: null } });
  await expect(page.getByText('产品启动或停止状态尚待确认', { exact: true })).toBeVisible();
  await expect(page.getByRole('button', { name: '启动产品', exact: true })).toHaveCount(0);
  await expect(page.getByRole('button', { name: '停止产品', exact: true })).toBeVisible();
});

test('a confirmed product with a failed export offers export retry without creating another Run', async ({ page, server }) => {
  await enter(page, server); const product = await createProduct(page, server);
  await fixture(server, 'product', { id: product.id, state: 'completed' });
  const runId = (await fixture(server, 'state')).products[0].run_id;
  await fixture(server, 'product', { id: product.id, state: 'blocked', finalization_error: true,
    blocking_reasons: ['代码已通过测试并交付 Git，但导出产品包失败。'] });
  const retry = page.getByRole('button', { name: '重试导出', exact: true });
  await expect(retry).toBeVisible();
  await expect(page.getByRole('button', { name: '重试准备', exact: true })).toHaveCount(0);
  await expect(page.getByRole('button', { name: '下载产品源码', exact: true })).toHaveCount(0);
  const request = page.waitForRequest(value => value.method() === 'POST' && value.url().endsWith(`/products/${product.id}/retry`));
  await retry.click();
  await request;
  await expect(retry).toHaveCount(0);
  const observed = await fixture(server, 'state');
  expect(observed.products).toHaveLength(1);
  expect(observed.products[0]).toMatchObject({ id: product.id, run_id: runId, state: 'running', finalization_error: false });
  expect(observed.retry_requests).toHaveLength(1);
  expect(observed.product_requests).toHaveLength(1);
  await fixture(server, 'product', { id: product.id, state: 'completed', delivery: true });
  await expect(page.getByRole('button', { name: '下载产品源码', exact: true })).toBeVisible();
});

test('rejected model configuration and endpoint edits clear sensitive input', async ({ page, server }) => {
  await enter(page, server, false);
  await page.getByRole('button', { name: '设置模型', exact: true }).click();
  const form = page.getByRole('form', { name: '设置研发模型' });
  await form.getByLabel('模型名称', { exact: true }).fill('reject-this-model');
  await form.getByLabel('API Key', { exact: true }).fill('fake-rejected-secret');
  await form.getByRole('button', { name: '保存并使用此模型' }).click();
  await expect(form.getByRole('alert')).toContainText('不支持所选研发用途');
  await expect(form.getByLabel('API Key', { exact: true })).toHaveValue('');
  await expect(page.locator('body')).not.toContainText('fake-rejected-secret');
  await form.getByLabel('API Key', { exact: true }).fill('do-not-reuse-on-another-endpoint');
  await form.getByLabel('接口地址', { exact: true }).fill('https://model.example/v1');
  await expect(form.getByLabel('API Key', { exact: true })).toHaveValue('');
  expect(JSON.stringify(await fixture(server, 'state'))).not.toContain('fake-rejected-secret');
});

test('create product layout remains usable on a narrow viewport', async ({ page, server }) => {
  await page.setViewportSize({ width: 390, height: 844 }); await enter(page, server);
  const form = await fillProduct(page);
  await expect(form.getByRole('button', { name: '开始创建产品' })).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth + 1)).toBeTruthy();
  await page.screenshot({ path: path.join(repository, 'tests/browser/artifacts/create-product-mobile.png'), fullPage: true });
});

test('configuration defaults initialize untouched fields and never replace edited product input', async ({ page, server }) => {
  await enter(page, server);
  const form = await fillProduct(page);
  await form.getByLabel('人工参与方式').selectOption('milestones');
  await fixture(server, 'setup', { product_defaults: { target: 'api', review_mode: 'every_step',
    language: 'en', max_model_requests: 75, output_root: '/configured/products', max_active_seconds: 1200, max_tool_calls: 50 } });
  await expect(form.getByLabel('API 服务')).toBeChecked();
  await expect(form.getByLabel('文档与代码注释语言', { exact: true })).toHaveValue('en');
  await expect(form.getByLabel('人工参与方式')).toHaveValue('milestones');
  await form.getByText('本次运行限额', { exact: true }).click();
  await expect(form.getByLabel('模型调用次数上限')).toHaveValue('75');
  await expect(form).toContainText('/configured/products');
  await form.getByLabel('Web 应用', { exact: false }).check();
  await form.getByLabel('API 服务', { exact: true }).uncheck();
  await form.getByLabel('模型调用次数上限').fill('0');
  await form.getByLabel('文档与代码注释语言', { exact: true }).selectOption('zh-CN');
  const reloaded = page.waitForResponse(async response => response.url().endsWith('/api/v1/product_setup')
    && (await response.json()).product_defaults?.max_model_requests === 120);
  await fixture(server, 'setup', { product_defaults: { target: 'api', review_mode: 'auto', language: 'en', max_model_requests: 120 } });
  await reloaded;
  await expect(form.getByLabel('Web 应用', { exact: false })).toBeChecked();
  await expect(form.getByLabel('人工参与方式')).toHaveValue('milestones');
  await expect(form.getByLabel('模型调用次数上限')).toHaveValue('0');
  await expect(form).toContainText('模型调用：不限次数');
  await expect(form.getByLabel('文档与代码注释语言', { exact: true })).toHaveValue('zh-CN');
  await form.getByRole('button', { name: '开始创建产品' }).click();
  await expect(page.getByText('正在准备研发环境与执行计划', { exact: true })).toBeVisible();
  expect((await fixture(server, 'state')).product_requests[0].payload).toMatchObject({
    targets: ['web'], review_mode: 'milestones', language: 'zh-CN', max_model_requests: 0,
  });
});

test('a configured zero initializes unlimited calls and later polling preserves an edited finite value', async ({ page, server }) => {
  await fixture(server, 'setup', { product_defaults: { max_model_requests: 0 } });
  await enter(page, server);
  const form = await fillProduct(page);
  await form.getByText('本次运行限额', { exact: true }).click();
  const input = form.getByLabel('模型调用次数上限');
  await expect(input).toHaveValue('0');
  await expect(input).toHaveAttribute('min', '0');
  await expect(form.getByRole('status')).toContainText('不限次数');
  await input.fill('7');
  const refreshed = page.waitForResponse(async response => response.url().endsWith('/api/v1/product_setup')
    && (await response.json()).product_defaults?.max_model_requests === 99);
  await fixture(server, 'setup', { product_defaults: { max_model_requests: 99 } });
  await refreshed;
  await expect(input).toHaveValue('7');
  await form.getByRole('button', { name: '开始创建产品' }).click();
  await expect(page.getByText('正在准备研发环境与执行计划', { exact: true })).toBeVisible();
  expect((await fixture(server, 'state')).product_requests[0].payload.max_model_requests).toBe(7);
});

test('pending file configuration requires restart before new products but still permits model edits', async ({ page, server }) => {
  await enter(page, server);
  const form = await fillProduct(page);
  const submit = form.getByRole('button', { name: '开始创建产品' });
  await expect(submit).toBeEnabled();
  await fixture(server, 'setup', { restart_required: true });
  await expect(page.getByText('配置已更新，需要重启平台', { exact: true })).toBeVisible();
  await expect(page.locator('body')).toContainText('agentflow stop');
  await expect(page.locator('body')).toContainText('agentflow start');
  await expect(submit).toBeDisabled();
  await page.getByRole('button', { name: '设置模型', exact: true }).click();
  const models = page.getByRole('form', { name: '设置研发模型' });
  await models.getByLabel('模型名称', { exact: true }).fill('browser-model-after-file-edit');
  await models.getByLabel('API Key', { exact: true }).fill('fixture-key-not-production');
  await models.getByRole('button', { name: '保存并使用此模型' }).click();
  await expect(models.getByText('模型配置已保存，正在重新检查可执行条件。', { exact: true })).toBeVisible();
  await expect(models.getByLabel('API Key', { exact: true })).toHaveValue('');
  await expect(submit).toBeDisabled();
  expect((await fixture(server, 'state')).product_requests).toHaveLength(0);
  await fixture(server, 'setup', { restart_required: false });
  await expect(submit).toBeEnabled();
});

test.describe('production product entry APIs without model execution', () => {
  test.use({ production: true });

  async function importExisting(page: Playwright.Page, server: Server, source = server.source_project!, language?: 'zh-CN' | 'en') {
    await enter(page, server, false);
    const form = await fillProduct(page, '已有事项产品');
    await form.getByLabel('导入已有项目', { exact: true }).check();
    if (language) await form.getByLabel('文档与代码注释语言', { exact: true }).selectOption(language);
    await form.getByLabel('已有项目目录', { exact: true }).fill(source);
    const diagnosed = page.waitForResponse(response => response.url().endsWith('/api/v1/products/diagnose') && response.request().method() === 'POST');
    await form.getByRole('button', { name: '识别项目平台', exact: true }).click();
    const diagnosis = await (await diagnosed).json();
    expect(diagnosis.diagnosis_kind).toBe('static');
    await expect(form.getByText('静态检查完成', { exact: true })).toBeVisible();
    const created = page.waitForResponse(response => response.url().endsWith('/api/v1/products') && response.request().method() === 'POST');
    await form.getByRole('button', { name: '登记已有项目', exact: true }).click();
    const response = await created;
    expect(response.status()).toBe(202);
    await expect(page.getByRole('heading', { name: '我的产品', exact: true })).toBeVisible();
    return { product: await response.json(), diagnosis };
  }

  test('my products is a separate management page and renaming changes no goal or source', async ({ page, server }) => {
    const sourceBefore = await readFile(path.join(server.source_project!, 'src/server.mjs'), 'utf8');
    const { product } = await importExisting(page, server);
    await expect(page.locator('.my-products-list-panel')).toContainText(product.name);
    await expect(page.getByRole('form', { name: '创建产品', exact: true })).not.toBeVisible();
    await page.locator('.my-products-actions').getByRole('button', { name: '改名', exact: true }).click();
    const dialog = page.getByRole('dialog');
    await dialog.getByLabel('新的产品名称').fill('改名后的阅读产品');
    const saved = page.waitForResponse(response => new URL(response.url()).pathname === `/api/v1/products/${product.id}`
      && response.request().method() === 'PATCH');
    await dialog.getByRole('button', { name: '保存更改', exact: true }).click();
    const response = await saved;
    expect(response.status()).toBe(200);
    expect(response.request().postDataJSON()).toEqual({ expected_revision: product.revision, name: '改名后的阅读产品' });
    await expect(page.locator('.product-progress').getByRole('heading', { name: '改名后的阅读产品', exact: true })).toBeVisible();
    const state = await fixture(server, 'state');
    expect(state.product[0]).toMatchObject({ id: product.id, name: '改名后的阅读产品', goal: product.goal, needs_restart: false });
    expect(state.model_invocation).toEqual([]); expect(state.run).toEqual([]);
    expect(await readFile(path.join(server.source_project!, 'src/server.mjs'), 'utf8')).toBe(sourceBefore);
    await page.getByRole('navigation', { name: '主导航' }).getByRole('button', { name: '创建产品', exact: true }).click();
    await expect(page.getByRole('form', { name: '创建产品', exact: true })).toBeVisible();
    await expect(page.locator('.my-products-list-panel')).not.toBeVisible();
  });

  test('critical product edits save without execution and only explicit restart starts from goal', async ({ page, server }) => {
    const { product } = await importExisting(page, server);
    const goal = '为个人阅读清单增加明确的搜索、归档和数据导出能力，并验证数据持久化。';
    await page.locator('.my-products-actions').getByRole('button', { name: '编辑配置', exact: true }).click();
    let dialog = page.getByRole('dialog');
    await dialog.getByLabel('长期产品目标').fill(goal);
    await dialog.getByRole('checkbox', { name: 'Web 应用', exact: true }).uncheck();
    await dialog.getByLabel('后续模型调用次数上限').fill('0');
    await expect(dialog).toContainText('保存本身不会调用模型');
    await dialog.getByRole('button', { name: '保存更改', exact: true }).click();
    await expect(page.getByText('配置已更新，必须从头运行', { exact: true })).toBeVisible();
    let state = await fixture(server, 'state');
    expect(state.product[0]).toMatchObject({ goal, targets: ['api'], max_model_requests: 0, needs_restart: true, config_revision: 2 });
    expect(state.product[0].initial_product_snapshot.goal).toBe(product.goal);
    expect(state.product_change).toEqual([]); expect(state.run).toEqual([]); expect(state.model_invocation).toEqual([]);
    await page.locator('.my-products-actions').getByRole('button', { name: '从头运行', exact: true }).click();
    dialog = page.getByRole('dialog');
    await expect(dialog).toContainText('从目标整理开始完整运行');
    await expect(dialog).toContainText(goal); await expect(dialog).toContainText('不限次数');
    const started = page.waitForResponse(response => response.url().endsWith(`/products/${product.id}/restart`) && response.request().method() === 'POST');
    await dialog.getByRole('button', { name: '开始从头运行', exact: true }).click();
    const response = await started;
    expect(response.status()).toBe(202);
    expect(await response.json()).toMatchObject({ product_id: product.id, kind: 'restart', state: 'preparing', start_stage: 'goal', config_revision: 2 });
    await expect(page.getByText('从头运行请求已接受，可查看准备状态和后续执行。', { exact: true })).toBeVisible();
    await expect(page.locator('.product-progress')).toContainText('正在准备从头运行，将从目标整理开始');
    await expect(page.locator('.product-progress')).not.toContainText('配置已更新，必须从头运行');
    await expect(page.locator('.my-products-list-panel')).not.toContainText('需从头运行');
    state = await fixture(server, 'state');
    expect(state.product_change).toHaveLength(1);
    expect(state.product_change[0].product_snapshot.goal).toBe(goal);
    expect(state.model_invocation).toEqual([]); expect(state.attempt).toEqual([]);
  });

  test('deleting and restoring a product keeps its source and never starts work', async ({ page, server }) => {
    await page.setViewportSize({ width: 390, height: 844 });
    const head = await readFile(path.join(server.source_project!, '.git/HEAD'), 'utf8');
    const source = await readFile(path.join(server.source_project!, 'src/server.mjs'), 'utf8');
    const { product } = await importExisting(page, server);
    await page.locator('.my-products-actions').getByRole('button', { name: '删除产品', exact: true }).click();
    let dialog = page.getByRole('dialog');
    await expect(dialog).toContainText('不会删除磁盘源码、交付目录或历史运行记录');
    await dialog.getByRole('button', { name: '移入已删除产品', exact: true }).click();
    await expect(page.getByRole('button', { name: '已删除产品', exact: true })).toHaveAttribute('aria-pressed', 'true');
    await expect(page.locator('.my-products-detail')).toContainText('磁盘源码、交付目录与历史记录仍保留');
    let state = await fixture(server, 'state');
    expect(state.product).toHaveLength(1); expect(state.product[0].deleted_at).toBeTruthy();
    await page.locator('.my-products-actions').getByRole('button', { name: '恢复产品', exact: true }).click();
    dialog = page.getByRole('dialog');
    await dialog.getByRole('button', { name: '恢复到我的产品', exact: true }).click();
    await expect(page.locator('.product-progress').getByRole('heading', { name: product.name, exact: true })).toBeVisible();
    state = await fixture(server, 'state');
    expect(state.product[0]).toMatchObject({ id: product.id, deleted_at: null, goal: product.goal, state: 'registered' });
    expect(state.run).toEqual([]); expect(state.model_invocation).toEqual([]);
    expect(await readFile(path.join(server.source_project!, '.git/HEAD'), 'utf8')).toBe(head);
    expect(await readFile(path.join(server.source_project!, 'src/server.mjs'), 'utf8')).toBe(source);
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth + 1)).toBeTruthy();
  });

  test('stale product edits stay visible and cannot overwrite a concurrent rename', async ({ page, server }) => {
    const { product } = await importExisting(page, server);
    await page.locator('.my-products-actions').getByRole('button', { name: '编辑配置', exact: true }).click();
    const dialog = page.getByRole('dialog');
    await dialog.getByLabel('长期产品目标').fill('这是一条等待版本核对的新目标，不应覆盖并发保存后的产品版本。');
    let first = true;
    await page.route(`**/api/v1/products/${product.id}`, async route => {
      if (first && route.request().method() === 'PATCH') {
        first = false;
        const other = await route.fetch({ headers: { ...route.request().headers(), 'idempotency-key': crypto.randomUUID() },
          postData: JSON.stringify({ expected_revision: product.revision, name: '另一处修改的名称' }) });
        expect(other.status()).toBe(200);
      }
      await route.continue();
    });
    await dialog.getByRole('button', { name: '保存更改', exact: true }).click();
    await expect(dialog.getByRole('alert')).toContainText('产品已变化');
    await expect(dialog.getByRole('button', { name: '保存更改', exact: true })).toBeDisabled();
    const state = await fixture(server, 'state');
    expect(state.product[0]).toMatchObject({ name: '另一处修改的名称', goal: product.goal, needs_restart: false });
    expect(state.product_change).toEqual([]); expect(state.model_invocation).toEqual([]);
  });

  test('lost restart acknowledgements replay the same operation after state changes', async ({ page, server }) => {
    const { product } = await importExisting(page, server);
    await page.locator('.my-products-actions').getByRole('button', { name: '从头运行', exact: true }).click();
    const dialog = page.getByRole('dialog');
    const observed: Playwright.Request[] = [];
    page.on('request', request => { if (request.method() === 'POST' && request.url().endsWith(`/products/${product.id}/restart`)) observed.push(request); });
    let first = true;
    await page.route(`**/products/${product.id}/restart`, async route => {
      if (first && route.request().method() === 'POST') { first = false; await route.fetch(); await route.abort(); }
      else await route.continue();
    });
    await dialog.getByRole('button', { name: '开始从头运行', exact: true }).click();
    await expect(dialog.getByRole('alert')).toContainText('操作结果尚未确认');
    await expect(dialog.getByRole('button', { name: '返回', exact: true })).toBeDisabled();
    await dialog.getByRole('button', { name: '核对原操作', exact: true }).click();
    await expect(dialog).toHaveCount(0);
    expect(observed).toHaveLength(2);
    expect(observed[0].headers()['idempotency-key']).toBe(observed[1].headers()['idempotency-key']);
    expect(observed[0].postDataJSON()).toEqual(observed[1].postDataJSON());
    const state = await fixture(server, 'state');
    expect(state.product_change).toHaveLength(1);
    expect(state.product_change[0]).toMatchObject({ product_id: product.id, kind: 'restart', start_stage: 'goal' });
    expect(state.model_invocation).toEqual([]);
  });

  test('new products support multiple platforms and native selection cannot start a run', async ({ page, server }) => {
    await enter(page, server, false);
    const form = await fillProduct(page, 'Web 与 API 产品');
    await expect(form.getByLabel('文档与代码注释语言', { exact: true })).toHaveValue('zh-CN');
    await form.getByLabel('文档与代码注释语言', { exact: true }).selectOption('en');
    await expect(page.getByRole('navigation', { name: '主导航' }).getByRole('button', { name: '项目与目标' })).toHaveCount(0);
    await expect(page.getByRole('navigation', { name: '主导航' }).getByRole('button', { name: '需求变更', exact: true })).toBeVisible();
    for (const platform of ['Web 应用', 'API 服务', 'iOS', 'Android', 'Windows', 'macOS', 'Linux']) {
      await expect(form.getByRole('checkbox', { name: platform, exact: true })).toBeVisible();
    }
    await form.getByRole('checkbox', { name: 'API 服务', exact: true }).check();
    for (const platform of ['iOS', 'Android', 'Windows', 'macOS', 'Linux']) {
      await form.getByRole('checkbox', { name: platform, exact: true }).check();
      await expect(form.getByRole('button', { name: '开始创建产品' })).toBeDisabled();
      await form.getByRole('checkbox', { name: platform, exact: true }).uncheck();
    }
    const sent = page.waitForResponse(response => response.url().endsWith('/api/v1/products') && response.request().method() === 'POST');
    await form.getByRole('button', { name: '开始创建产品' }).click();
    const response = await sent;
    expect(response.status()).toBe(202);
    expect(response.request().postDataJSON()).toMatchObject({ targets: ['web', 'api'], creation_mode: 'new' });
    const stored = await fixture(server, 'state');
    expect(stored.product).toHaveLength(1);
    expect(stored.product[0]).toMatchObject({ targets: ['web', 'api'], state: 'preparing', language: 'en', initial_language: 'en' });
    expect(stored.run).toEqual([]); expect(stored.attempt).toEqual([]); expect(stored.check).toEqual([]);
    expect(stored.model_invocation).toEqual([]); expect(stored.delivery).toEqual([]);
  });

  test('zero request count is submitted as an unlimited integer while invalid counts cannot submit', async ({ page, server }) => {
    await enter(page, server, false);
    const form = await fillProduct(page, '不限次数入口产品');
    await form.getByText('本次运行限额', { exact: true }).click();
    const input = form.getByLabel('模型调用次数上限');
    for (const invalid of ['-1', '0.5', '2001']) {
      await input.fill(invalid);
      await expect(form.getByRole('button', { name: '开始创建产品' })).toBeDisabled();
    }
    await input.fill('0');
    await expect(form.getByRole('status')).toContainText('不限次数');
    const response = page.waitForResponse(value => value.url().endsWith('/api/v1/products') && value.request().method() === 'POST');
    await form.getByRole('button', { name: '开始创建产品' }).click();
    const accepted = await response;
    expect(accepted.status()).toBe(202);
    expect(accepted.request().postDataJSON().max_model_requests).toBe(0);
    expect((await accepted.json()).max_model_requests).toBe(0);
    const stored = await fixture(server, 'state');
    expect(stored.product[0]).toMatchObject({ max_model_requests: 0, max_tool_calls: 100, max_active_seconds: 1800 });
    expect(stored.run).toEqual([]); expect(stored.model_invocation).toEqual([]);
  });

  test('import diagnoses a real repository, registers its original goal, and runs no project code', async ({ page, server }) => {
    const originalHead = await readFile(path.join(server.source_project!, '.git/HEAD'), 'utf8');
    const { product, diagnosis } = await importExisting(page, server);
    expect(diagnosis.detected_targets).toEqual(expect.arrayContaining(['web', 'api']));
    expect(diagnosis.git_detected).toBeTruthy();
    expect(product).toMatchObject({ creation_mode: 'import', project_path: server.source_project, state: 'registered' });
    await expect(page.getByText('已有项目已登记，填写需求变更后开始下一次迭代', { exact: true })).toBeVisible();
    expect(await readFile(path.join(server.source_project!, '.git/HEAD'), 'utf8')).toBe(originalHead);
    const stored = await fixture(server, 'state');
    expect(stored.product).toHaveLength(1); expect(stored.run).toEqual([]);
    expect(stored.attempt).toEqual([]); expect(stored.model_invocation).toEqual([]); expect(stored.check).toEqual([]);
  });

  test('a small requirement starts at PRD and preserves the product goal', async ({ page, server }) => {
    const { product } = await importExisting(page, server);
    await page.locator('.product-progress').getByRole('button', { name: '需求变更', exact: true }).click();
    await expect(page.getByRole('heading', { name: '需求变更', exact: true })).toBeVisible();
    const form = page.getByRole('form', { name: '需求变更', exact: true });
    await expect(form.getByLabel('选择产品')).toHaveValue(product.id);
    await expect(page.locator('.requirements-product-context')).toContainText(product.goal);
    await expect(page.locator('.requirements-process')).toContainText('检查架构与接口');
    await form.getByLabel('需求标题（可选）').fill('归档筛选');
    const changeText = '在事项列表增加归档筛选，默认仅显示未归档事项；切换后能够查看并恢复已归档项。';
    await form.getByLabel('这次要新增或修改什么', { exact: true }).fill(changeText);
    const acceptance = '归档和恢复后列表立即更新，切换筛选不丢失尚未提交的输入。';
    await form.getByLabel('验收标准（可选）').fill(acceptance);
    const saved = page.waitForResponse(response => response.url().endsWith(`/products/${product.id}/changes`) && response.request().method() === 'POST');
    await form.getByRole('button', { name: '提交需求变更', exact: true }).click();
    const response = await saved;
    expect(response.status()).toBe(202);
    expect(response.request().postDataJSON()).toMatchObject({ title: '归档筛选', description: changeText,
      acceptance_criteria: acceptance, expected_revision: product.revision });
    expect(response.request().postDataJSON()).not.toHaveProperty('goal');
    await expect(page.locator('.requirements-change')).toContainText('归档筛选');
    await expect(page.locator('.requirements-change')).toContainText(acceptance);
    const stored = await fixture(server, 'state');
    expect(stored.product[0].goal).toBe(product.goal);
    expect(stored.product_change).toHaveLength(1);
    expect(stored.product_change[0]).toMatchObject({ description: changeText, acceptance_criteria: acceptance,
      start_stage: 'prd', architecture_policy: 'review_existing', state: 'preparing' });
    expect(stored.run).toEqual([]); expect(stored.attempt).toEqual([]); expect(stored.model_invocation).toEqual([]);
  });

  test('native project markers can be registered but never unlock requirement execution', async ({ page, server }) => {
    const { product, diagnosis } = await importExisting(page, server, server.multi_project!);
    expect(diagnosis.detected_targets).toEqual(expect.arrayContaining(['web', 'api', 'ios', 'android', 'macos']));
    expect(diagnosis.execution_supported).toBeFalsy();
    expect(product).toMatchObject({ creation_mode: 'import', state: 'blocked', execution_supported: false });
    await page.locator('.product-progress').getByRole('button', { name: '需求变更', exact: true }).click();
    const form = page.getByRole('form', { name: '需求变更', exact: true });
    await form.getByLabel('这次要新增或修改什么', { exact: true }).fill('为原生客户端增加归档列表筛选功能，并保留原有事项数据。');
    await expect(form).toContainText('原生平台暂不支持启动研发');
    await expect(form.getByRole('button', { name: '提交需求变更', exact: true })).toBeDisabled();
    const stored = await fixture(server, 'state');
    expect(stored.product_change).toEqual([]); expect(stored.run).toEqual([]); expect(stored.model_invocation).toEqual([]);
  });

  test('an unrecognized import keeps an empty platform selection instead of guessing Web', async ({ page, server }) => {
    const { product, diagnosis } = await importExisting(page, server, server.unknown_project!);
    expect(diagnosis.detected_targets).toEqual([]);
    expect(product).toMatchObject({ target: null, targets: [], execution_supported: false, state: 'blocked' });
    await page.getByRole('navigation', { name: '主导航' }).getByRole('button', { name: '创建产品', exact: true }).click();
    const form = page.getByRole('form', { name: '创建产品', exact: true });
    await expect(form.getByRole('checkbox', { name: 'Web 应用', exact: true })).not.toBeChecked();
    await expect(form.getByRole('checkbox', { name: 'API 服务', exact: true })).not.toBeChecked();
    const stored = await fixture(server, 'state');
    expect(stored.product).toHaveLength(1); expect(stored.run).toEqual([]); expect(stored.model_invocation).toEqual([]);
  });

  test('active accepted requirements keep language settings unavailable while preserving their snapshot', async ({ page, server }) => {
    const { product } = await importExisting(page, server, server.source_project!, 'en');
    expect(product.language).toBe('en');
    await page.locator('.product-progress').getByRole('button', { name: '需求变更', exact: true }).click();
    const requirement = page.getByRole('form', { name: '需求变更', exact: true });
    await expect(requirement.getByLabel('本次文档与代码注释语言', { exact: true })).toHaveValue('en');
    await requirement.getByLabel('这次要新增或修改什么', { exact: true }).fill('增加归档筛选，并保持原有的创建和恢复功能。');
    await requirement.getByRole('button', { name: '提交需求变更', exact: true }).click();
    await expect(page.locator('.requirements-change')).toContainText('文档与注释：English');
    await page.getByRole('navigation', { name: '主导航' }).getByRole('button', { name: '我的产品', exact: true }).click();
    const progress = page.locator('.product-progress');
    await expect(progress.getByText('正在准备研发环境与执行计划', { exact: true })).toBeVisible();
    await progress.locator('summary').filter({ hasText: '文档与代码注释语言' }).click();
    const settings = progress.getByRole('form', { name: '设置产品语言', exact: true });
    await expect(progress).toContainText('已接受的运行使用各自的语言快照');
    await expect(settings.getByLabel('后续迭代默认语言')).toBeDisabled();
    await expect(settings.getByRole('button', { name: '保存语言设置', exact: true })).toBeDisabled();
    const stored = await fixture(server, 'state');
    expect(stored.product[0]).toMatchObject({ goal: product.goal, language: 'en', initial_language: 'en' });
    expect(stored.product_change[0].language).toBe('en');
    expect(stored.attempt).toEqual([]); expect(stored.model_invocation).toEqual([]);
  });

  test('a lost language-setting acknowledgement preserves its revision and operation key', async ({ page, server }) => {
    const { product } = await importExisting(page, server);
    const progress = page.locator('.product-progress');
    await progress.locator('summary').filter({ hasText: '文档与代码注释语言' }).click();
    const form = progress.getByRole('form', { name: '设置产品语言', exact: true });
    await form.getByLabel('后续迭代默认语言').selectOption('en');
    const requests: Playwright.Request[] = [];
    page.on('request', request => { if (request.method() === 'POST' && request.url().endsWith(`/products/${product.id}/language`)) requests.push(request); });
    let first = true;
    await page.route(`**/products/${product.id}/language`, async route => {
      if (first && route.request().method() === 'POST') { first = false; await route.fetch(); await route.abort(); }
      else await route.continue();
    });
    await form.getByRole('button', { name: '保存语言设置', exact: true }).click();
    await expect(form.getByRole('alert')).toContainText('操作结果尚未确认');
    await expect(form.getByLabel('后续迭代默认语言')).toBeDisabled();
    await form.getByRole('button', { name: '核对语言设置', exact: true }).click();
    await expect(form.getByRole('status')).toContainText('English');
    expect(requests).toHaveLength(2);
    expect(requests[0].postDataJSON()).toEqual(requests[1].postDataJSON());
    expect(requests[0].headers()['idempotency-key']).toBe(requests[1].headers()['idempotency-key']);
    const stored = await fixture(server, 'state');
    expect(stored.product[0]).toMatchObject({ language: 'en', revision: product.revision + 1, goal: product.goal });
    expect(stored.run).toEqual([]); expect(stored.model_invocation).toEqual([]);
  });

  test('requirement acknowledgement retry keeps the captured revision and idempotency key', async ({ page, server }) => {
    const { product } = await importExisting(page, server);
    await page.getByRole('navigation', { name: '主导航' }).getByRole('button', { name: '创建产品', exact: true }).click();
    const entry = page.getByRole('form', { name: '创建产品', exact: true });
    await entry.getByLabel('产品名称', { exact: true }).fill('原生参考产品');
    await entry.getByLabel('已有项目目录', { exact: true }).fill(server.multi_project!);
    await entry.getByRole('button', { name: '识别项目平台', exact: true }).click();
    await expect(entry.getByText('静态检查完成', { exact: true })).toBeVisible();
    const registered = page.waitForResponse(response => response.url().endsWith('/api/v1/products') && response.request().method() === 'POST');
    await entry.getByRole('button', { name: '登记已有项目', exact: true }).click();
    const other = await (await registered).json();
    await expect(page.locator('.product-progress').getByRole('heading', { name: '原生参考产品', exact: true })).toBeVisible();
    await page.locator('.product-progress').getByRole('button', { name: '需求变更', exact: true }).click();
    const form = page.getByRole('form', { name: '需求变更', exact: true });
    await expect(form.getByLabel('选择产品')).toHaveValue(other.id);
    await form.getByLabel('选择产品').selectOption(product.id);
    await form.getByLabel('这次要新增或修改什么', { exact: true }).fill('增加归档列表过滤器，并保持原有创建与恢复事项行为不变。');
    await form.getByLabel('验收标准（可选）').fill('归档项可以恢复；重复操作不会新增另一条事项。');
    const submitted: Playwright.Request[] = [];
    page.on('request', request => { if (request.method() === 'POST' && request.url().endsWith(`/products/${product.id}/changes`)) submitted.push(request); });
    let lost = true;
    await page.route(`**/products/${product.id}/changes`, async route => {
      if (route.request().method() === 'POST' && lost) { lost = false; await route.fetch(); await route.abort(); }
      else await route.continue();
    });
    await form.getByRole('button', { name: '提交需求变更', exact: true }).click();
    await expect(form.getByRole('alert')).toContainText('操作结果尚未确认');
    await expect(form.getByLabel('这次要新增或修改什么', { exact: true })).toBeDisabled();
    await form.getByRole('button', { name: '核对原需求提交', exact: true }).click();
    await expect(page.locator('.requirements-change')).toHaveCount(1);
    await expect(form.getByLabel('选择产品')).toHaveValue(product.id);
    expect(submitted).toHaveLength(2);
    expect(submitted[0].postDataJSON()).toEqual(submitted[1].postDataJSON());
    expect(submitted[0].headers()['idempotency-key']).toBe(submitted[1].headers()['idempotency-key']);
    const stored = await fixture(server, 'state');
    expect(stored.product_change).toHaveLength(1); expect(stored.product_change[0].product_id).toBe(product.id);
    expect(stored.product.find((item: { id: string }) => item.id === product.id).goal).toBe(product.goal);
    expect(stored.model_invocation).toEqual([]);
  });

  test('changing the import path invalidates diagnosis and the requirements page fits a narrow screen', async ({ page, server }) => {
    await page.setViewportSize({ width: 390, height: 844 });
    await enter(page, server, false);
    const form = await fillProduct(page);
    await form.getByLabel('导入已有项目', { exact: true }).check();
    await form.getByLabel('已有项目目录', { exact: true }).fill(server.source_project!);
    await form.getByRole('button', { name: '识别项目平台', exact: true }).click();
    await expect(form.getByText('静态检查完成', { exact: true })).toBeVisible();
    await form.getByLabel('已有项目目录', { exact: true }).fill(path.join(server.directory, 'other-project'));
    await expect(form.getByText('静态检查完成', { exact: true })).toHaveCount(0);
    await expect(form.getByRole('button', { name: '登记已有项目', exact: true })).toBeDisabled();
    await page.getByRole('navigation', { name: '主导航' }).getByRole('button', { name: '需求变更', exact: true }).click();
    await expect(page.getByText('先创建或导入一个产品', { exact: true })).toBeVisible();
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth + 1)).toBeTruthy();
  });
});
