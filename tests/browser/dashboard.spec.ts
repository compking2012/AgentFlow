import type * as Playwright from '../../apps/dashboard/node_modules/@playwright/test/index.js';
import { createRequire } from 'node:module';
import { spawn } from 'node:child_process';
import { mkdtemp, readFile, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import path from 'node:path';

// Tests live outside the frontend package; require its one installed Playwright instance.
const { test: base, expect } = createRequire(import.meta.url)('../../apps/dashboard/node_modules/@playwright/test/index.js') as typeof Playwright;
type Page = Playwright.Page;
type Server = { origin: string; bootstrap: string; fixture_key: string; directory: string; approval_id: string; fingerprint: string; run: string; item: string; node_fingerprint?: string };
const repository = path.resolve(import.meta.dirname, '../..');

async function exposeOwnerApi(page: Page) {
  const { buildSync } = createRequire(import.meta.url)('../../apps/dashboard/node_modules/esbuild');
  const result = buildSync({ entryPoints: [path.join(repository, 'apps/dashboard/src/api.ts')],
    bundle: true, format: 'esm', write: false });
  await page.route('**/__test-owner-api.js', route => route.fulfill({ contentType: 'text/javascript',
    body: result.outputFiles[0].text }));
}

const test = base.extend<{ server: Server; executor: boolean }>({
  executor: [false, { option: true }],
  server: async ({ page, executor }, use) => {
    const directory = await mkdtemp(path.join(tmpdir(), 'agentflow-browser-'));
    const process = spawn(path.join(repository, '.venv/bin/python'), ['-u', 'tests/browser/server.py', '--directory', directory, ...(executor ? ['--executor'] : [])], {
      cwd: repository, env: { ...globalThis.process.env, PYTHONPATH: path.join(repository, 'src') }, stdio: ['ignore', 'pipe', 'pipe'],
    });
    let errorOutput = ''; process.stderr.on('data', data => { errorOutput += String(data); });
    try {
      const server = await new Promise<Server>((resolve, reject) => {
        let text = ''; const timeout = setTimeout(() => reject(new Error(`Fixture service timed out: ${errorOutput}`)), 20_000);
        process.on('exit', code => { clearTimeout(timeout); reject(new Error(`Fixture service exited ${code}: ${errorOutput}`)); });
        process.stdout.on('data', data => {
          text += String(data);
          const line = text.split('\n').find(value => value.startsWith('{"origin"'));
          if (line) { clearTimeout(timeout); resolve(JSON.parse(line)); }
        });
      });
      await expect.poll(async () => { try { return (await fetch(`${server.origin}/health`)).status; } catch { return 0; } }).toBe(200);
      await use(server);
    } finally {
      await page.goto('about:blank').catch(() => undefined);
      process.kill('SIGTERM');
      await new Promise<void>(resolve => { if (process.exitCode !== null) resolve(); else { const timeout = setTimeout(() => { process.kill('SIGKILL'); resolve(); }, 5000); process.once('exit', () => { clearTimeout(timeout); resolve(); }); } });
      await rm(directory, { recursive: true, force: true });
    }
  },
});

async function enter(page: Page, server: Server) {
  await page.goto(`${server.origin}/#bootstrap=${encodeURIComponent(server.bootstrap)}`);
  await expect(page.getByRole('heading', { name: '创建产品', exact: true })).toBeVisible();
}

async function state(server: Server) {
  const response = await fetch(`${server.origin}/__fixture/state`, { headers: { 'X-Fixture-Key': server.fixture_key } });
  expect(response.ok).toBeTruthy(); return response.json();
}

async function quality(server: Server, mode: string, delivery = false) {
  const response = await fetch(`${server.origin}/__fixture/quality`, { method: 'POST', headers: {
    'X-Fixture-Key': server.fixture_key, Origin: server.origin, 'Idempotency-Key': crypto.randomUUID(),
    'Content-Type': 'application/json',
  }, body: JSON.stringify({ mode, delivery }) });
  expect(response.ok).toBeTruthy();
}

async function capture(page: Page, name: string) {
  await page.evaluate(() => window.scrollTo(0, 0));
  await page.screenshot({ path: path.join(globalThis.process.env.AGENTFLOW_BROWSER_CAPTURE_DIR ?? path.join(repository, 'tests/browser/artifacts'), `${name}.png`), fullPage: true });
}

test('inactive pages stop polling and returning to creation preserves the draft', async ({ page, server }) => {
  const requests: string[] = [];
  page.on('request', request => requests.push(new URL(request.url()).pathname));
  await enter(page, server);
  await page.getByLabel('产品名称', { exact: true }).fill('保留这份草稿');
  await page.getByRole('button', { name: '产物与质量', exact: true }).click();
  await expect(page.getByTestId('target-ios_native')).toBeVisible();
  await page.getByRole('button', { name: '创建产品', exact: true }).click();
  await expect(page.getByLabel('产品名称', { exact: true })).toHaveValue('保留这份草稿');
  requests.length = 0;
  // Cross a full 2.5-second polling interval after the view has been hidden.
  await page.waitForTimeout(3000);
  expect(requests.filter(url => /\/(workflow|quality_summary|recovery_options|target_matrix|checks|candidates|deliveries)$/.test(url))).toEqual([]);
  await page.getByRole('button', { name: '执行台', exact: true }).click();
  await expect(page.locator('.workflow-map')).toBeVisible();
});

test('bootstrap is removed; limited tab ticket restores the page without storing owner credentials', async ({ page, server }) => {
  const requests: { url: string; headers: Record<string, string> }[] = [];
  page.on('request', request => { requests.push({ url: request.url(), headers: request.headers() }); });
  await enter(page, server);
  expect(new URL(page.url()).hash).toBe('');
  expect(await page.evaluate(() => ({ local: localStorage.length, session: sessionStorage.length, cookie: document.cookie }))).toEqual({ local: 0, session: 1, cookie: '' });
  expect(await page.context().cookies()).toEqual([]);
  const authenticated = requests.filter(r => r.headers.authorization);
  expect(authenticated.length).toBeGreaterThan(0);
  expect(authenticated.every(r => new URL(r.url).origin === server.origin)).toBeTruthy();
  expect(requests.every(r => !r.url.includes(server.bootstrap))).toBeTruthy();
  const ticket = await page.evaluate(() => sessionStorage.getItem(`agentflow.browser-session.v1:${location.origin}`));
  expect(ticket).toBeTruthy();
  expect(authenticated.every(request => request.headers.authorization !== `Bearer ${ticket}`)).toBeTruthy();
  await expect(page.getByRole('button', { name: '锁定工作台' })).toHaveCount(0);
  await page.reload();
  await expect(page.getByRole('heading', { name: '创建产品', exact: true })).toBeVisible();
  expect(requests.some(request => request.url.endsWith('/api/v1/session/resume') && request.headers.authorization === `Bearer ${ticket}`)).toBeTruthy();
  expect(requests.filter(request => !request.url.endsWith('/api/v1/session/resume')).every(request => request.headers.authorization !== `Bearer ${ticket}`)).toBeTruthy();
});

test('zero request quota displays unlimited calls without inventing zero fees', async ({ page, server }) => {
  async function setLimit(max_model_requests: number) {
    const response = await fetch(`${server.origin}/__fixture/request_count`, { method: 'POST', headers: {
      'X-Fixture-Key': server.fixture_key, Origin: server.origin, 'Idempotency-Key': crypto.randomUUID(),
      'Content-Type': 'application/json',
    }, body: JSON.stringify({ max_model_requests }) });
    expect(response.ok).toBeTruthy();
  }
  await setLimit(0);
  await enter(page, server);
  await page.getByRole('button', { name: '执行台', exact: true }).click();
  const facts = page.locator('.run-facts');
  await expect(facts).toContainText('不限次数 · 金额按供应商计费');
  await expect(facts).not.toContainText('0 次');
  await expect(facts).not.toContainText('0.00');
  await setLimit(1);
  await page.getByRole('button', { name: '刷新数据', exact: true }).click();
  await expect(facts).toContainText('1 次 · 金额按供应商计费');
});

test('quality displays seven actual evidence states and renders artifact text without executing HTML', async ({ page, server }) => {
  await enter(page, server);
  await page.getByRole('button', { name: '产物与质量', exact: true }).click();
  await expect(page.getByTestId('target-ios_native')).toContainText('未执行');
  await expect(page.getByTestId('target-ios_native')).toContainText('尚无执行证据');
  await expect(page.getByText('尚无已确认交付', { exact: true })).toBeVisible();
  for (const target of ['web', 'api', 'ios_native', 'android_native', 'windows_native', 'macos_native', 'linux_native']) {
    await expect(page.getByTestId(`target-${target}`)).not.toContainText('有效通过');
  }
  await page.getByRole('button', { name: /^阅读 / }).first().click();
  await expect(page.locator('.artifact-preview')).toContainText('<script>window.__artifactExecuted=true</script>');
  expect(await page.evaluate(() => (window as any).__artifactExecuted)).toBeUndefined();
  await capture(page, 'quality');
});

test('nested matrix joins only current candidate checks and requires every mandatory entry to have real evidence', async ({ page, server }) => {
  await quality(server, 'wrong_fingerprint');
  await enter(page, server);
  await page.getByRole('button', { name: '产物与质量', exact: true }).click();
  const web = page.getByTestId('target-web');
  const api = page.getByTestId('target-api');
  await expect(web).toContainText('2 项必检');
  await expect(web).toContainText('未执行');
  await expect(api).not.toContainText('有效通过');
  await quality(server, 'partial');
  await page.getByRole('button', { name: '刷新数据' }).click();
  await expect(web).toContainText('验证未完成');
  await expect(web).toContainText('1 项证据有效');
  await expect(api).toContainText('存在失败');
  await quality(server, 'unverified');
  await page.getByRole('button', { name: '刷新数据' }).click();
  await expect(api).toContainText('验证未完成');
  await expect(web).toContainText('0 项证据有效');
  await quality(server, 'empty');
  await page.getByRole('button', { name: '刷新数据' }).click();
  await expect(web).toContainText('验证未完成');
  await expect(api).not.toContainText('有效通过');
  await quality(server, 'passed');
  await page.getByRole('button', { name: '刷新数据' }).click();
  await expect(web).toContainText('有效通过');
  await expect(api).toContainText('有效通过');
  await expect(page.getByTestId('target-ios_native')).toContainText('未执行');
  await expect(page.getByText('尚无已确认交付', { exact: true })).toBeVisible();
  await quality(server, 'retired');
  await page.getByRole('button', { name: '刷新数据' }).click();
  await expect(web).toContainText('未执行');
  await expect(api).not.toContainText('有效通过');
});

test('actual confirmed delivery receipt is recognized without a fabricated delivered status field', async ({ page, server }) => {
  await quality(server, 'passed', true);
  await enter(page, server);
  await page.getByRole('button', { name: '产物与质量', exact: true }).click();
  await expect(page.getByText('存在已交付记录', { exact: true })).toBeVisible();
  const receipt = page.locator('summary').filter({ hasText: '已确认交付' });
  await expect(receipt).toHaveCount(1);
  await receipt.click();
  await expect(receipt.locator('..')).toContainText(`refs/heads/codex/agentflow/${server.run}`);
  await expect(receipt.locator('..')).toContainText('确认时间');
  await expect(receipt.locator('..')).not.toContainText('状态未提供');
});

test('artifact preview rejects content larger than its bounded reader limit', async ({ page, server }) => {
  const response = await fetch(`${server.origin}/__fixture/large_artifact`, { method: 'POST', headers: {
    'X-Fixture-Key': server.fixture_key, Origin: server.origin, 'Idempotency-Key': crypto.randomUUID(),
  } });
  expect(response.ok).toBeTruthy();
  await enter(page, server);
  await page.getByRole('button', { name: '产物与质量', exact: true }).click();
  await page.getByRole('button', { name: /^阅读 / }).first().click();
  await expect(page.getByRole('alert')).toContainText('下载');
  await expect(page.locator('.artifact-preview')).toHaveCount(0);
});

test('switching runs clears earlier artifacts, previews and evidence', async ({ page, server }) => {
  await quality(server, 'passed', true);
  await enter(page, server);
  await page.getByRole('button', { name: '产物与质量', exact: true }).click();
  await page.getByRole('button', { name: /^阅读 / }).first().click();
  await expect(page.locator('.artifact-preview')).toContainText('权限规则需要修复');
  const response = await fetch(`${server.origin}/__fixture/empty_run`, { method: 'POST', headers: {
    'X-Fixture-Key': server.fixture_key, Origin: server.origin, 'Idempotency-Key': crypto.randomUUID(),
  } });
  expect(response.ok).toBeTruthy();
  const next = await response.json();
  await page.getByRole('button', { name: '刷新数据' }).click();
  await page.getByRole('combobox', { name: '需求变更', exact: true }).selectOption('run:' + next.id);
  await expect(page.getByText('尚无已登记产物', { exact: true })).toBeVisible();
  await expect(page.locator('.artifact-preview')).toHaveCount(0);
  await expect(page.getByText('尚无已确认交付', { exact: true })).toBeVisible();
  await expect(page.getByTestId('target-web')).not.toContainText('有效通过');
});

test('approval sends the captured revision/fingerprint and does not erase a failed quality result', async ({ page, server }) => {
  await enter(page, server);
  await page.getByRole('button', { name: /人工审核/ }).first().click();
  await page.getByRole('button', { name: '查看并审核' }).click();
  const dialog = page.getByRole('dialog');
  await expect(dialog).toContainText(server.fingerprint);
  const request = page.waitForRequest(r => r.url().includes('/decisions') && r.method() === 'POST');
  await dialog.getByRole('button', { name: '提交审核决定' }).click();
  expect((await request).postDataJSON()).toMatchObject({ decision: 'approve', expected_revision: 1, expected_fingerprint: server.fingerprint });
  await expect(page.getByRole('status').filter({ hasText: '审核决定已保存' })).toBeVisible();
  const current = await state(server);
  expect(current.approval.decision).toBe('approve');
  expect(current.item.quality_result).toBe('failed');
});

test('rejection requires reason and change expectation and persists a real revision', async ({ page, server }) => {
  await enter(page, server);
  await page.getByRole('button', { name: /人工审核/ }).first().click();
  await page.getByRole('button', { name: '查看并审核' }).click();
  const dialog = page.getByRole('dialog');
  await dialog.getByLabel('审核决定').selectOption('reject');
  await expect(dialog.getByRole('button', { name: '提交审核决定' })).toBeDisabled();
  await dialog.getByLabel('驳回原因').fill('权限校验遗漏');
  await dialog.getByLabel('需要怎样修改').fill('补充普通用户修改他人工单时的拒绝行为及证据。');
  await capture(page, 'approval');
  await dialog.getByRole('button', { name: '提交审核决定' }).click();
  await expect(page.getByRole('status').filter({ hasText: '驳回意见已保存' })).toBeVisible();
  const current = await state(server);
  expect(current.approval.decision).toBe('reject');
  expect(current.item.generation).toBe(2);
  expect(current.decisions[0].change_expectation).toContain('普通用户');
});

test('stale approval stays disabled instead of silently upgrading its fingerprint', async ({ page, server }) => {
  await enter(page, server);
  await page.getByRole('button', { name: /人工审核/ }).first().click();
  await page.getByRole('button', { name: '查看并审核' }).click();
  const response = await fetch(`${server.origin}/__fixture/supersede`, { method: 'POST', headers: {
    'X-Fixture-Key': server.fixture_key, Origin: server.origin, 'Idempotency-Key': crypto.randomUUID(),
  } });
  expect(response.ok).toBeTruthy();
  await expect(page.getByRole('dialog')).toContainText('审核对象已更新或处理');
  await expect(page.getByRole('dialog').getByRole('button', { name: '提交审核决定' })).toBeDisabled();
  expect((await state(server)).approval.decision).toBeNull();
});

test('advanced settings preserve configuration evidence and show missing node configuration', async ({ page, server }) => {
  await enter(page, server);
  await page.getByRole('button', { name: '本地设置', exact: true }).click();
  await expect(page.getByText('等待确认模型版本', { exact: true })).toBeVisible();
  await expect(page.getByText('执行通道尚未配置。', { exact: false })).toBeVisible();
  await page.getByText('创建一次性节点配对', { exact: true }).click();
  await expect(page.getByRole('button', { name: '创建配对码', exact: true })).toBeDisabled();
  expect(await page.context().cookies()).toEqual([]);
  await capture(page, 'settings');
});

test('execution view shows stage nodes without a second work-item table and remains usable on a narrow screen', async ({ page, server }) => {
  await enter(page, server);
  await page.getByRole('button', { name: '执行台', exact: true }).click();
  await expect(page.getByRole('list', { name: '研发阶段流程' })).toBeVisible();
  await expect(page.getByRole('heading', { name: '工作项与依赖', exact: true })).toHaveCount(0);
  await expect(page.getByTestId('stage-code_review')).toContainText('目标产物');
  await expect(page.getByText('代码审查发现权限规则缺陷', { exact: true })).toBeVisible();
  await capture(page, 'execution');
  await page.setViewportSize({ width: 390, height: 844 });
  await expect(page.getByRole('heading', { name: '执行台', exact: true })).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth + 1)).toBeTruthy();
  await capture(page, 'mobile');
});

async function expandedWorkflow(server: Server) {
  const response = await fetch(`${server.origin}/__fixture/workflow`, { method: 'POST', headers: {
    'X-Fixture-Key': server.fixture_key, Origin: server.origin, 'Idempotency-Key': crypto.randomUUID(),
  } });
  expect(response.ok).toBeTruthy(); return response.json();
}

async function workflowFixture(server: Server, endpoint: string, value: unknown = {}) {
  const response = await fetch(`${server.origin}/__fixture/${endpoint}`, { method: 'POST', headers: {
    'X-Fixture-Key': server.fixture_key, Origin: server.origin, 'Idempotency-Key': crypto.randomUUID(),
    'Content-Type': 'application/json',
  }, body: JSON.stringify(value) });
  expect(response.ok).toBeTruthy(); return response.json();
}

test('rework remains inside one stage and historical failures do not override the final result', async ({ page, server }) => {
  await workflowFixture(server, 'full_workflow');
  const graph = await workflowFixture(server, 'logical_repairs');
  await enter(page, server);
  await page.getByRole('button', { name: '执行台', exact: true }).click();
  const nodes = page.locator('.workflow-map-node');
  await expect(nodes).toHaveCount(16);
  const review = page.getByTestId('stage-code_review');
  await expect(review).toHaveCount(1);
  await expect(review).toHaveAttribute('data-tone', 'complete');
  const implementation = page.getByTestId('stage-implementation');
  await expect(implementation).toHaveCount(1);
  await expect(implementation).toHaveAttribute('data-stage-id', graph.stage_ids.implementation);
  await expect(page.getByTestId('stage-integration_test_implementation')).toHaveCount(1);
  const nodeIds = await nodes.evaluateAll(elements => elements.map(e => e.getAttribute('data-stage-id')));
  const edges = await page.locator('.workflow-map-link').evaluateAll(elements => elements.map(e => ({
    from: e.getAttribute('data-from'), to: e.getAttribute('data-to'),
  })));
  expect(edges.every(edge => nodeIds.indexOf(edge.from) < nodeIds.indexOf(edge.to))).toBeTruthy();
  await review.click();
  const history = page.locator('.workflow-inspector-history');
  await expect(history).toBeVisible();
  await expect(history).not.toHaveAttribute('open', '');
  await expect(page.locator('.workflow-inspector-blockers')).toHaveCount(0);
  await history.getByText(/执行历史/).click();
  await expect(history).toContainText('审查未通过');
  await workflowFixture(server, 'logical_repairs', { status: 'running' });
  await expect(implementation).toHaveAttribute('data-tone', 'active');
  await expect(implementation).toHaveAttribute('data-stage-id', graph.stage_ids.implementation);
  await page.setViewportSize({ width: 390, height: 844 });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1)).toBeTruthy();
  await capture(page, 'workflow-logical-stages');
});

test('full workflow wraps with actual dependency arrows, active highlighting and stable keyboard selection', async ({ page, server }) => {
  const fixture = await workflowFixture(server, 'full_workflow');
  await enter(page, server);
  await page.getByRole('button', { name: '执行台', exact: true }).click();
  const canvas = page.locator('.workflow-map-canvas');
  const nodes = canvas.locator('.workflow-map-node');
  const inspector = page.getByRole('region', { name: '阶段详情', exact: true });
  await expect(nodes).toHaveCount(16);
  await expect.poll(() => canvas.evaluate(element => element.clientWidth === element.parentElement!.clientWidth)).toBeTruthy();
  const columns = Number(await canvas.getAttribute('data-columns'));
  expect(columns).toBeGreaterThanOrEqual(3);
  expect(Number(await canvas.getAttribute('data-rows'))).toBeGreaterThan(1);
  const positions = await nodes.evaluateAll(elements => elements.map(element => ({ x: element.getBoundingClientRect().x, y: element.getBoundingClientRect().y })));
  expect(positions[1].x).toBeGreaterThan(positions[0].x);
  expect(positions[columns].y).toBeGreaterThan(positions[0].y);
  expect(Math.abs(positions[columns].x - positions[0].x)).toBeLessThan(2);
  const actualEdges = await canvas.locator('.workflow-map-link').evaluateAll(elements => elements.map(element => `${element.getAttribute('data-from')}:${element.getAttribute('data-to')}`).sort());
  const expectedEdges = Object.entries(fixture.dependencies as Record<string, string[]>).flatMap(([step, previous]) => previous.map(id => `${id}:${fixture.stage_ids[step]}`)).sort();
  expect(actualEdges).toEqual(expectedEdges);
  expect(await canvas.locator('.workflow-map-link[data-wrap="true"]').count()).toBeGreaterThan(0);
  expect(await canvas.locator('.workflow-map-link').evaluateAll(elements => elements.every(element =>
    Boolean(element.getAttribute('marker-end')) && !element.getAttribute('d')!.includes('NaN')))).toBeTruthy();
  const implementation = page.getByTestId('stage-implementation');
  await expect(implementation).toHaveAttribute('data-tone', 'active');
  await expect(inspector).toContainText('选择一个阶段');
  await expect(canvas.locator('.artifact-card')).toHaveCount(0);
  await capture(page, 'workflow-full-desktop');

  const review = page.getByTestId('stage-code_review');
  await review.click();
  await expect(review).toHaveAttribute('aria-pressed', 'true');
  await expect(inspector).toContainText('代码审查发现阻塞问题，请查看审查意见。');
  await expect(page.getByTestId('stage-unit_test_plan')).toContainText('待人工审核');
  await expect(page.getByTestId('stage-unit_test_implementation')).toContainText('已阻塞');
  await workflowFixture(server, 'workflow_state', { step: 'implementation', status: 'completed' });
  await workflowFixture(server, 'workflow_state', { step: 'unit_test_implementation', status: 'running' });
  await expect(page.getByTestId('stage-unit_test_implementation')).toHaveAttribute('data-tone', 'active');
  await expect(review).toHaveAttribute('aria-pressed', 'true');
  await expect(inspector.getByRole('heading', { name: '代码审查', exact: true })).toBeVisible();

  await review.focus();
  await page.keyboard.press('Home');
  await expect(nodes.nth(0)).toBeFocused();
  await page.keyboard.press('ArrowRight');
  await expect(nodes.nth(1)).toBeFocused();
  await page.keyboard.press('Enter');
  await expect(nodes.nth(1)).toHaveAttribute('aria-pressed', 'true');
  await page.keyboard.press('End');
  await expect(nodes.nth(15)).toBeFocused();
  await page.keyboard.press('Space');
  await expect(nodes.nth(15)).toHaveAttribute('aria-pressed', 'true');
  await page.keyboard.press('Escape');
  await expect(inspector).toContainText('选择一个阶段');
  await page.keyboard.press('ArrowUp');
  await expect(nodes.nth(15 - columns)).toBeFocused();

  await page.setViewportSize({ width: 390, height: 844 });
  await expect.poll(async () => Number(await canvas.getAttribute('data-columns'))).toBeLessThan(columns);
  await expect.poll(() => canvas.evaluate(element => element.clientWidth === element.parentElement!.clientWidth)).toBeTruthy();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth + 1)).toBeTruthy();
  expect(await page.locator('.workflow-map-viewport').evaluate(element => element.scrollWidth <= element.clientWidth + 1)).toBeTruthy();
  await page.getByRole('button', { name: '定位执行中', exact: false }).click();
  await expect(page.getByTestId('stage-unit_test_implementation')).toHaveAttribute('aria-pressed', 'true');
  await expect(inspector.getByRole('heading', { name: '单元测试编写', exact: true })).toBeVisible();
  await expect.poll(() => page.getByTestId('stage-unit_test_implementation').evaluate(element => {
    const node = element.getBoundingClientRect();
    const viewport = element.closest('.workflow-map-viewport')!.getBoundingClientRect();
    return node.top >= viewport.top && node.bottom <= viewport.bottom;
  })).toBeTruthy();
  await capture(page, 'workflow-full-mobile');
});

test('repair review explains initial approval and retained plans in the same workflow nodes', async ({ page, server }) => {
  await workflowFixture(server, 'full_workflow');
  await workflowFixture(server, 'repair_context');
  await enter(page, server);
  await page.getByRole('button', { name: '执行台', exact: true }).click();
  const review = page.getByTestId('stage-code_review');
  await expect(review).toHaveCount(1);
  await expect(review).toHaveAttribute('data-tone', 'danger');
  await expect(review).toContainText('首次审查已通过');
  await expect(review).toContainText('当前修复复审未通过');
  await review.click();
  const context = page.locator('.workflow-inspector-context');
  await expect(context).toContainText('修复后复审 第1轮');
  await expect(context).toContainText('首次审查已通过 · 当前修复复审未通过');
  await page.locator('.workflow-inspector-history > summary').click();
  await expect(page.locator('.workflow-inspector-history')).toContainText('已完成');
  for (const step of ['unit_test_plan', 'integration_test_strategy']) {
    const plan = page.getByTestId(`stage-${step}`);
    await expect(plan).toHaveCount(1);
    await expect(plan).toContainText('已完成，继续沿用');
    await plan.click();
    await expect(context).toContainText('已完成，继续沿用');
    await expect(page.locator('.workflow-inspector-output .artifact-card')).toHaveCount(1);
  }
  for (const [mode, text] of [['pending', '当前修复复审待执行'], ['running', '当前修复复审执行中'], ['passed', '当前修复复审已通过']]) {
    await workflowFixture(server, 'repair_context', { mode });
    await expect(review).toContainText(text);
  }
  await workflowFixture(server, 'repair_context');
  await page.setViewportSize({ width: 390, height: 844 });
  await review.click();
  await expect(context).toContainText('当前修复复审未通过');
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1)).toBeTruthy();
  expect(await review.evaluate(element => element.scrollHeight <= element.clientHeight + 1)).toBeTruthy();
  await capture(page, 'workflow-repair-context-mobile');
  await workflowFixture(server, 'workflow_state', { step: 'unit_test_plan', status: 'pending' });
  await expect(page.getByTestId('stage-unit_test_plan')).not.toContainText('继续沿用');
  await workflowFixture(server, 'repair_context', { mode: 'rebound' });
  await expect(review).toHaveAttribute('data-tone', 'complete');
  await expect(review).not.toContainText('当前修复复审');
  const unitReview = page.getByTestId('stage-unit_test_implementation:review');
  await expect(unitReview).toHaveCount(1);
  await expect(unitReview).toContainText('首次审查已通过');
  await expect(unitReview).toContainText('当前修复复审待执行');
  await expect(page.getByTestId('stage-unit_test_plan')).toContainText('已完成，继续沿用');
  await unitReview.click();
  await expect(context).toContainText('修复后复审 第1轮');
  await workflowFixture(server, 'repair_context', { mode: 'own_phase' });
  await expect(review).toHaveAttribute('data-tone', 'complete');
  await expect(review).not.toContainText('当前修复复审');
  await expect(unitReview).toHaveAttribute('data-tone', 'complete');
  const integrationReview = page.getByTestId('stage-integration_test_implementation:review');
  await expect(integrationReview).toHaveCount(1);
  await expect(integrationReview).toContainText('首次审查已通过');
  await expect(integrationReview).toContainText('当前修复复审待执行');
  await integrationReview.click();
  await expect(context).toContainText('修复后复审 第1轮');
  await expect(page.locator('.workflow-inspector-heading')).toContainText('集成测试代码审查');
});

test('selecting a map node shows one stage artifact, parallel tasks and safe readable Markdown', async ({ page, server }) => {
  const seeded = await expandedWorkflow(server);
  await enter(page, server);
  await page.getByRole('button', { name: '执行台', exact: true }).click();
  const stage = page.getByTestId('stage-research');
  await expect(page.locator('.run-facts')).toContainText('模型请求上限');
  await expect(page.locator('.run-facts')).toContainText('99 次 · 金额按供应商计费');
  await expect(page.locator('.run-facts')).not.toContainText('USD 0.00');
  await expect(stage).toContainText('市场与竞品调研报告');
  await expect(page.locator('.workflow-map-canvas .artifact-card')).toHaveCount(0);
  await stage.click();
  await expect(stage).toHaveAttribute('aria-pressed', 'true');
  const inspector = page.getByRole('region', { name: '阶段详情', exact: true });
  const output = inspector.locator('.workflow-inspector-output');
  await expect(output.locator('.artifact-card')).toHaveCount(1);
  await expect(output).toContainText('市场与竞品调研报告.md');
  await expect(inspector.locator('.workflow-map-task')).toHaveCount(2);
  await expect(inspector.getByText('用户与市场调研', { exact: true })).toBeVisible();
  await expect(inspector.getByText('竞品功能分析', { exact: true })).toBeVisible();
  await expect(inspector.locator('.workflow-inspector-aggregation')).toContainText('调研分析汇总');
  await inspector.locator('.workflow-map-task').first().locator(':scope > summary').click();
  await expect(inspector.locator('.workflow-map-task').first().locator('.artifact-card')).toBeVisible();
  await output.getByRole('button', { name: /^阅读 / }).click();
  const preview = page.getByRole('region', { name: '文档阅读区' });
  await expect(preview.getByRole('heading', { name: '范围', exact: true })).toBeVisible();
  await expect(preview.getByRole('table')).toContainText('持久化');
  await expect(preview).toContainText('<script>window.__workflowExecuted=true</script>');
  expect(await page.evaluate(() => (window as any).__workflowExecuted)).toBeUndefined();
  await expect(preview.locator('a[href^="javascript:"]')).toHaveCount(0);
  await expect(page.locator('body')).not.toContainText('openhands_final.json');
  const downloading = page.waitForEvent('download');
  const requested = page.waitForRequest(request => request.url().includes('/api/v1/readable_artifacts/') && request.url().includes('download=true'));
  await output.getByRole('button', { name: /^下载 / }).click();
  const download = await downloading;
  expect(download.suggestedFilename()).toBe('市场与竞品调研报告.md');
  expect(await readFile((await download.path())!, 'utf8')).toContain('| 持久化 | 必需 |');
  expect(Boolean((await requested).headers().authorization)).toBeTruthy();
  await page.getByTestId('stage-unit_test_implementation').click();
  await expect(page.getByRole('region', { name: '文档阅读区' })).toHaveCount(0);
  const codeStage = inspector.locator('.workflow-inspector-output');
  await expect(codeStage).toContainText(path.join(seeded.code_directory, 'tests'));
  await expect(codeStage.getByRole('button', { name: /^查看说明 / })).toBeVisible();
  await expect(codeStage.getByRole('button', { name: /^下载说明 / })).toBeVisible();
  await capture(page, 'workflow-expanded');
  await page.setViewportSize({ width: 390, height: 844 });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth + 1)).toBeTruthy();
});

test('review completion requires a passed verdict across nodes, details and work counts', async ({ page, server }) => {
  await workflowFixture(server, 'full_workflow');
  await workflowFixture(server, 'review_workflow', { mode: 'failed' });
  await enter(page, server);
  await page.getByRole('button', { name: '执行台', exact: true }).click();
  const stage = page.getByTestId('stage-code_review');
  const inspector = page.getByRole('region', { name: '阶段详情', exact: true });
  const headingStates = inspector.locator('.workflow-inspector-states');
  const finding = inspector.locator('.workflow-map-task').filter({ hasText: '权限边界审查' }).locator(':scope > summary');
  const aggregation = inspector.locator('.workflow-inspector-aggregation > div > header');
  const count = page.locator('.metric').filter({ hasText: '任务完成' }).locator('strong');
  await stage.click();
  await expect(stage).toHaveAttribute('data-status', 'completed');
  await expect(stage).toHaveAttribute('data-tone', 'danger');
  for (const surface of [headingStates, finding, aggregation, stage]) {
    await expect(surface).not.toContainText('步骤完成');
    await expect(surface).toContainText('审查未通过');
  }
  await expect(finding.locator('.status-danger')).toBeVisible();
  await expect(inspector.locator('.workflow-inspector-tasks > header')).toContainText('1 / 2 完成');
  await expect(count).toHaveText('7 / 18');
  await expect(page.locator('.workflow-map-toolbar')).toContainText('6 个完成');
  await capture(page, 'workflow-review-failed');

  await workflowFixture(server, 'review_workflow', { mode: 'unknown' });
  for (const surface of [stage, headingStates, finding, aggregation]) await expect(surface).toContainText('审查待确认');
  await expect(stage).toHaveAttribute('data-tone', 'attention');
  await expect(count).toHaveText('7 / 18');

  for (const [mode, label, tone] of [['pending', '待执行', 'pending'], ['running', '执行中', 'active']]) {
    await workflowFixture(server, 'review_workflow', { mode });
    for (const surface of [stage, headingStates, finding, aggregation]) await expect(surface).toContainText(label);
    await expect(stage).toHaveAttribute('data-tone', tone);
    await expect(count).toHaveText('7 / 18');
  }

  await workflowFixture(server, 'review_workflow', { mode: 'waiting_approval' });
  await expect(stage).toContainText('待审 · 未通过');
  await expect(headingStates).toContainText('待审 · 未通过');
  await expect(count).toHaveText('7 / 18');

  await workflowFixture(server, 'review_workflow', { mode: 'passed' });
  await expect(stage).toHaveAttribute('data-tone', 'complete');
  for (const surface of [stage, headingStates, finding, aggregation]) await expect(surface).toContainText('已完成');
  await expect(inspector.locator('.workflow-inspector-tasks > header')).toContainText('2 / 2 完成');
  await expect(count).toHaveText('9 / 18');
  await expect(page.locator('.workflow-map-toolbar')).toContainText('7 个完成');
});

test('failed stages stay active and discoverable while sibling work is running, queued or stopping', async ({ page, server }) => {
  await workflowFixture(server, 'full_workflow');
  await workflowFixture(server, 'workflow_mixed', { active_status: 'running' });
  await enter(page, server);
  await page.getByRole('button', { name: '执行台', exact: true }).click();
  const stage = page.getByTestId('stage-implementation');
  const inspector = page.getByRole('region', { name: '阶段详情', exact: true });
  await expect(stage).toHaveAttribute('data-status', 'failed');
  await expect(stage).toHaveAttribute('data-tone', 'danger');
  await expect(stage).toHaveAttribute('data-active', 'true');
  await expect(stage).toContainText('仍有活跃任务');
  await expect(page.locator('.workflow-map-toolbar')).toContainText('1 个活跃');
  await page.getByRole('button', { name: '定位执行中', exact: false }).click();
  await expect(stage).toHaveAttribute('aria-pressed', 'true');
  await expect(inspector).toContainText(/模型(?:单次输出|响应)达到(?:输出)?上限/);
  await expect(inspector).toContainText('页面交互实现');
  await capture(page, 'workflow-mixed-status');
  for (const status of ['waiting_execution', 'cancel_requested']) {
    await workflowFixture(server, 'workflow_mixed', { active_status: status });
    const label = status === 'waiting_execution' ? '等待节点执行' : '正在停止';
    await expect(inspector.locator('.workflow-map-task').filter({ hasText: '页面交互实现' })).toContainText(label);
    await expect(stage).toHaveAttribute('data-status', 'failed');
    await expect(stage).toHaveAttribute('data-active', 'true');
    await expect(page.locator('.workflow-map-toolbar')).toContainText('1 个活跃');
  }
  await workflowFixture(server, 'workflow_mixed', { active_status: 'completed' });
  await expect(stage).toHaveAttribute('data-active', 'false');
  await expect(stage).toHaveAttribute('data-tone', 'danger');
  await expect(page.locator('.workflow-map-toolbar')).toContainText('0 个活跃');
  await expect(inspector).toContainText(/模型(?:单次输出|响应)达到(?:输出)?上限/);
});

test('quality uses verified case rates and explicit measurements while retaining issue categories', async ({ page, server }) => {
  const seeded = await expandedWorkflow(server);
  await quality(server, 'partial');
  await enter(page, server);
  await page.getByRole('button', { name: '产物与质量', exact: true }).click();
  const unit = page.getByRole('region', { name: '单元测试', exact: true });
  const integration = page.getByRole('region', { name: '集成测试', exact: true });
  const performance = page.getByRole('region', { name: '性能测量', exact: true });
  await expect(unit).toContainText('100%');
  await expect(unit).toContainText('4 通过');
  await expect(unit.locator('.quality-test-passed')).toContainText('测试通过');
  await expect(integration).toContainText('待齐备');
  await expect(integration).toContainText('1 失败');
  await expect(integration.locator('.status-danger')).toContainText('存在失败');
  await expect(integration).toContainText('必需测试尚未全部完成');
  await expect(unit).toContainText('测试耗时');
  await expect(performance).toContainText('未测');
  await expect(page.locator('.quality-bugs')).toContainText('安全');
  await expect(page.locator('.quality-bugs')).toContainText('未分类');
  await expect(page.locator('.quality-bugs')).toContainText('历史未分类事项');
  await expect(page.getByRole('region', { name: '代码审查问题', exact: true })).toContainText('关闭情况未登记');
  await expect(page.locator('.quality-directories')).toContainText(path.join(seeded.code_directory, 'tests'));
  await quality(server, 'passed');
  await expect(integration).toContainText('100%');
  await expect(integration).toContainText('4 通过');
  await expect(integration.locator('.quality-test-passed')).toContainText('测试通过');
  await expect(performance).toContainText('未测');
  await quality(server, 'measured');
  await expect(performance).toContainText('1 项');
  await expect(page.locator('.quality-performance')).toContainText('接口响应 P95');
  await expect(page.locator('.quality-performance')).toContainText('42.5');
  await expect(page.locator('.quality-performance')).toContainText('ms');
  await expect(page.locator('.quality-performance')).toContainText('20 个样本');
  await expect(page.locator('.quality-performance')).toContainText('测试报告测量');
  await capture(page, 'quality-readable');
  await quality(server, 'unverified');
  await expect(unit).toContainText('未测');
  await expect(integration).toContainText('未测');
  await expect(performance).toContainText('未测');
});

test('pause records its reason and continuation waits for verified recovery prerequisites', async ({ page, server }) => {
  await enter(page, server);
  await page.getByRole('button', { name: '执行台', exact: true }).click();
  await page.getByRole('button', { name: '暂停运行', exact: true }).click();
  const dialog = page.getByRole('dialog');
  await dialog.getByLabel('操作原因').fill('检查当前缺陷');
  const submitted = page.waitForRequest(request => request.method() === 'POST' && request.url().endsWith(`/runs/${server.run}/control`));
  await dialog.getByRole('button', { name: '确认操作' }).click();
  expect((await submitted).postDataJSON()).toMatchObject({ action: 'pause', reason: '检查当前缺陷' });
  // This fixture has a failed review and no model budget accounts; continuation
  // must not bypass them. Successful paused continuation has its own real API test.
  await expect(page.getByRole('button', { name: '继续运行', exact: true })).toBeDisabled();
  await expect(page.locator('.recovery-blockers')).toContainText('恢复前需要处理');
});

test('approval dialog traps focus and Escape returns focus to its trigger', async ({ page, server }) => {
  await enter(page, server);
  await page.getByRole('button', { name: /人工审核/ }).first().click();
  const trigger = page.getByRole('button', { name: '查看并审核' });
  await trigger.click();
  const dialog = page.getByRole('dialog');
  await expect(dialog.getByLabel('审核意见（可选）')).toBeFocused();
  await dialog.getByRole('button', { name: '提交审核决定' }).focus();
  await page.keyboard.press('Tab');
  await expect(dialog.getByRole('button', { name: '关闭审核' })).toBeFocused();
  await page.keyboard.press('Escape');
  await expect(dialog).toHaveCount(0);
  await expect(trigger).toBeFocused();
});

test.describe('configured node management', () => {
  test.use({ executor: true });
  test('creates pairing against the real NodeService without persisting the one-time code in browser storage', async ({ page, server }) => {
    await enter(page, server);
    await page.getByRole('button', { name: '本地设置', exact: true }).click();
    await page.getByText('创建一次性节点配对', { exact: true }).click();
    const form = page.getByRole('form', { name: '节点配对' });
    await form.getByLabel('节点名称').fill('UI 配对节点');
    await form.getByLabel('节点公钥指纹').fill(server.node_fingerprint!);
    await form.getByLabel('API 服务', { exact: true }).check();
    await form.getByRole('button', { name: '创建配对码' }).click();
    await expect(page.getByText('一次性配对码已创建', { exact: true })).toBeVisible();
    await page.getByRole('button', { name: '显示配对码', exact: true }).click();
    const code = await page.locator('.pairing-secret').textContent();
    expect(code!.length).toBeGreaterThanOrEqual(32);
    expect(page.url()).not.toContain(code!);
    const current = await state(server);
    expect(current.pairings).toHaveLength(1);
    expect(current.pairings[0].expected_node_public_key_fingerprint).toBe(server.node_fingerprint);
    expect(JSON.stringify(current.pairings)).not.toContain(code!);
    expect(await page.evaluate(() => [localStorage.length, sessionStorage.length, document.cookie])).toEqual([0, 1, '']);
    await page.getByRole('button', { name: '关闭并清除', exact: true }).click();
    await expect(page.locator('.pairing-secret')).toHaveCount(0);
  });
});


test('expired owner authentication is renewed transparently without reissuing bootstrap or locking', async ({ page, server }) => {
  await enter(page, server);
  let rejected = false; let renewals = 0;
  page.on('request', request => { if (request.url().endsWith('/api/v1/session/resume')) renewals += 1; });
  await page.route('**/api/v1/meta', async route => {
    if (!rejected) {
      rejected = true;
      await route.fulfill({ status: 401, contentType: 'application/json', body: JSON.stringify({ error: { code: 'unauthorized', message: 'Expired fixture owner bearer' } }) });
    } else await route.continue();
  });
  await page.getByRole('button', { name: '刷新数据', exact: true }).click();
  await expect.poll(() => renewals).toBe(1);
  await expect(page.getByRole('heading', { name: '创建产品', exact: true })).toBeVisible();
  await expect(page.getByRole('heading', { name: '打开本机工作台' })).toHaveCount(0);
  await expect(page.getByRole('button', { name: '锁定工作台' })).toHaveCount(0);
});

test('direct local access works in a new tab and after a stale browser ticket', async ({ page, server }) => {
  await page.goto(server.origin);
  await expect(page.getByRole('heading', { name: '创建产品', exact: true })).toBeVisible();
  expect(page.url()).not.toContain('bootstrap');
  expect(await page.evaluate(() => [localStorage.length, sessionStorage.length, document.cookie])).toEqual([0, 1, '']);
  await page.evaluate(() => sessionStorage.setItem(`agentflow.browser-session.v1:${location.origin}`, 'stale-controller-ticket'));
  await page.reload();
  await expect(page.getByRole('heading', { name: '创建产品', exact: true })).toBeVisible();
  expect(await page.evaluate(() => sessionStorage.getItem(`agentflow.browser-session.v1:${location.origin}`))).not.toBe('stale-controller-ticket');
  const fresh = await page.context().browser()!.newContext();
  try {
    const tab = await fresh.newPage();
    await tab.goto(server.origin);
    await expect(tab.getByRole('heading', { name: '创建产品', exact: true })).toBeVisible();
  } finally { await fresh.close(); }
});

test('direct local access reconnects without optional tab storage', async ({ page, server }) => {
  await page.addInitScript(() => {
    Storage.prototype.getItem = () => { throw new Error('storage unavailable'); };
    Storage.prototype.setItem = () => { throw new Error('storage unavailable'); };
    Storage.prototype.removeItem = () => { throw new Error('storage unavailable'); };
  });
  await page.goto(server.origin);
  await expect(page.getByRole('heading', { name: '创建产品', exact: true })).toBeVisible();
  await page.reload();
  await expect(page.getByRole('heading', { name: '创建产品', exact: true })).toBeVisible();
});

test('rejected session renewal establishes local authority before retrying a command once with its original key', async ({ page, server }) => {
  await exposeOwnerApi(page);
  await page.goto(server.origin);
  await expect(page.getByRole('heading', { name: '创建产品', exact: true })).toBeVisible();
  let commandCount = 0; let localCount = 0; const keys: string[] = [];
  await page.route('**/api/v1/session/resume', route => route.fulfill({ status: 401, contentType: 'application/json',
    body: JSON.stringify({ error: { code: 'unauthorized', message: 'Previous controller ended' } }) }));
  page.on('request', request => { if (request.url().endsWith('/api/v1/session/local')) localCount += 1; });
  await page.route('**/api/v1/projects', route => {
    if (route.request().method() !== 'POST') return route.continue();
    commandCount += 1; keys.push(route.request().headers()['idempotency-key']);
    return route.fulfill({ status: commandCount === 1 ? 401 : 200, contentType: 'application/json',
      body: JSON.stringify(commandCount === 1 ? { error: { code: 'unauthorized', message: 'Expired owner' } } : { connected: true }) });
  });
  const result = await page.evaluate(async () => {
    const modulePath = '/__test-owner-api.js';
    const { OwnerApi } = await import(modulePath);
    const api = new OwnerApi(); await api.restore();
    return api.command('/api/v1/projects', { name: 'Safe replay' }, 'original-command-key');
  });
  expect(result).toEqual({ connected: true });
  expect(commandCount).toBe(2); expect(keys).toEqual(['original-command-key', 'original-command-key']);
  expect(localCount).toBe(2);
});

for (const failure of ['forbidden', 'network', 'unknown-401', 'renew-forbidden', 'second-401']) {
  test(`local session does not repeat commands after ${failure}`, async ({ page, server }) => {
    await exposeOwnerApi(page);
    await page.goto(server.origin);
    await expect(page.getByRole('heading', { name: '创建产品', exact: true })).toBeVisible();
    let posts = 0; let localPosts = 0; let renewals = 0;
    page.on('request', request => { if (request.url().endsWith('/api/v1/session/local')) localPosts += 1; });
    if (failure === 'renew-forbidden') {
      await page.route('**/api/v1/session/resume', route => ++renewals === 1 ? route.continue() :
        route.fulfill({ status: 403, contentType: 'application/json',
          body: JSON.stringify({ error: { code: 'forbidden', message: 'Wrong scope' } }) }));
    }
    await page.route('**/api/v1/projects', route => {
      if (route.request().method() !== 'POST') return route.continue();
      posts += 1;
      if (failure === 'network') return route.abort('connectionfailed');
      return route.fulfill({ status: failure === 'forbidden' ? 403 : 401, contentType: 'application/json',
        body: JSON.stringify({ error: { code: failure === 'forbidden' ? 'forbidden' : failure === 'unknown-401' ? 'business_unknown' : 'unauthorized', message: 'Not accepted' } }) });
    });
    const error = await page.evaluate(async () => {
      const modulePath = '/__test-owner-api.js';
      const { OwnerApi } = await import(modulePath); const api = new OwnerApi(); await api.restore();
      try { await api.command('/api/v1/projects', {}, 'no-duplicate-command'); return null; }
      catch (error) { return (error as { code: string }).code; }
    });
    expect(error).toBeTruthy(); expect(posts).toBe(failure === 'second-401' ? 2 : 1);
    expect(localPosts).toBe(0);
  });
}

test('local connection failure can be retried from the page without a startup link', async ({ page, server }) => {
  let blocked = true;
  await page.route('**/api/v1/session/local', route => blocked ? route.abort('connectionfailed') : route.continue());
  await page.goto(server.origin);
  await expect(page.getByRole('button', { name: '重新连接', exact: true })).toBeVisible();
  blocked = false;
  await page.getByRole('button', { name: '重新连接', exact: true }).click();
  await expect(page.getByRole('heading', { name: '创建产品', exact: true })).toBeVisible();
});

test('an explicitly opened retained history remains selected when its product leaves the active list', async ({ page, server }) => {
  await enter(page, server);
  await page.getByRole('button', { name: '执行台', exact: true }).click();
  await expect(page.getByRole('combobox', { name: '需求变更', exact: true })).toHaveValue('run:' + server.run);
  await page.route('**/api/v1/runs', route => route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ items: [] }) }));
  await page.getByRole('button', { name: '刷新数据', exact: true }).click();
  await expect(page.getByRole('combobox', { name: '需求变更', exact: true })).toHaveValue('run:' + server.run);
  await expect(page.getByRole('combobox', { name: '需求变更', exact: true }).getByRole('option', { selected: true })).toContainText('检查权限规则');
  await expect(page.locator('.workflow-panel')).toContainText('检查权限规则');
});

test('project then requirement selectors preserve history, full inherited prefix and selected run actions', async ({ page, server }) => {
  const seeded = await workflowFixture(server, 'project_versions');
  await enter(page, server);
  await page.getByRole('button', { name: '执行台', exact: true }).click();
  const project = page.getByRole('combobox', { name: '项目', exact: true });
  const version = page.getByRole('combobox', { name: '需求变更', exact: true });
  await project.selectOption('product:' + seeded.product_id);
  await expect(version).toHaveValue('change:' + seeded.changes[1]);
  await expect(version.locator('option')).toHaveText(['初始开发', '需求变更 1 · 筛选收藏', '需求变更 2 · 导出列表']);
  await expect(page.locator('.workflow-map-node')).toHaveCount(17);
  await expect(page.getByTestId('stage-goal')).toContainText('完成 · 沿用上版');
  const stableIds = await page.locator('.workflow-map-node').evaluateAll(nodes => nodes.map(node => node.getAttribute('data-stage-id')));
  await version.selectOption('change:' + seeded.changes[0]);
  await expect(page.getByTestId('stage-prd')).toContainText('待人工审核');
  expect(await page.locator('.workflow-map-node').evaluateAll(nodes => nodes.map(node => node.getAttribute('data-stage-id')))).toEqual(stableIds);
  await page.getByTestId('stage-goal').click();
  await expect(page.locator('.workflow-inspector-provenance')).toContainText('本次运行不重新执行');
  await expect(page.locator('.workflow-inspector-tasks')).toHaveCount(0);
  await expect(page.locator('.workflow-map-inspector').getByRole('button', { name: /重新运行/ })).toHaveCount(0);
  await workflowFixture(server, 'project_versions', { product_id: seeded.product_id });
  await page.getByRole('button', { name: '刷新数据', exact: true }).click();
  await expect(version.locator('option')).toHaveCount(4);
  await expect(version).toHaveValue('change:' + seeded.changes[0]);
  await page.getByRole('button', { name: '暂停运行', exact: true }).click();
  const dialog = page.getByRole('dialog');
  await dialog.getByLabel('操作原因').fill('确认操作绑定历史选择');
  const control = page.waitForRequest(request => request.method() === 'POST' && request.url().endsWith('/control'));
  await dialog.getByRole('button', { name: '确认操作', exact: true }).click();
  expect((await control).url()).toBe(`${server.origin}/api/v1/runs/${seeded.run_ids[1]}/control`);
  await expect(dialog).toBeHidden();
  await workflowFixture(server, 'project_versions', { product_id: seeded.product_id, deleted: true });
  await page.getByRole('button', { name: '刷新数据', exact: true }).click();
  await expect(project.locator('option:checked')).toContainText('已删除项目历史');
  await expect(version).toHaveValue('change:' + seeded.changes[0]);
  await project.selectOption('product:' + seeded.imported_id);
  await expect(version).toHaveValue('initial');
  await expect(page.getByTestId('stage-research')).toContainText('完成 · 沿用已有项目基线');
  await expect(page.getByRole('button', { name: '暂停运行', exact: true })).toHaveCount(0);
  await page.getByTestId('stage-research').click();
  await expect(page.locator('.workflow-inspector-provenance')).toContainText('未开展市场或竞品调研');
  await capture(page, 'project-workflow-imported-baseline');
});

test('a delayed old workflow response cannot overwrite a different selected project', async ({ page, server }) => {
  const seeded = await workflowFixture(server, 'project_versions');
  let release!: () => void;
  const gate = new Promise<void>(resolve => { release = resolve; });
  let seen!: () => void;
  const arrived = new Promise<void>(resolve => { seen = resolve; });
  await page.route(`**/api/v1/runs/${seeded.run_ids[1]}/workflow`, async route => {
    const response = await route.fetch(); seen(); await gate; await route.fulfill({ response });
  });
  try {
    await enter(page, server);
    await page.getByRole('button', { name: '执行台', exact: true }).click();
    const project = page.getByRole('combobox', { name: '项目', exact: true });
    await project.selectOption('product:' + seeded.product_id);
    await page.getByRole('combobox', { name: '需求变更', exact: true }).selectOption('change:' + seeded.changes[0]);
    await arrived;
    await project.selectOption('product:' + seeded.imported_id);
    await expect(page.getByTestId('stage-research')).toContainText('沿用已有项目基线');
    release();
    await page.getByRole('button', { name: '刷新数据', exact: true }).click();
    await expect(project).toHaveValue('product:' + seeded.imported_id);
    await expect(page.getByTestId('stage-research')).toContainText('沿用已有项目基线');
    await expect(page.getByTestId('stage-prd')).toContainText('待执行');
    await expect(page.getByRole('button', { name: '暂停运行', exact: true })).toHaveCount(0);
  } finally { release(); }
});
