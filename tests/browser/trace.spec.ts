import type * as Playwright from '../../apps/dashboard/node_modules/@playwright/test/index.js';
import { createRequire } from 'node:module';
import { spawn } from 'node:child_process';
import { mkdtemp, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import path from 'node:path';

const { test: base, expect } = createRequire(import.meta.url)('../../apps/dashboard/node_modules/@playwright/test/index.js') as typeof Playwright;
type Server = { origin: string; bootstrap: string; run: string; item: string; fixture_key: string };
type Event = { id: string; seq: number; kind: string; title: string; content: string; created_at: string;
  duration_ms?: number; redacted?: boolean; truncated?: boolean };
const repository = path.resolve(import.meta.dirname, '../..');
const test = base.extend<{ server: Server }>({
  server: async ({ page }, use) => {
    const directory = await mkdtemp(path.join(tmpdir(), 'agentflow-trace-ui-'));
    const child = spawn(path.join(repository, '.venv/bin/python'), ['-u', 'tests/browser/server.py', '--directory', directory], {
      cwd: repository, env: { ...process.env, PYTHONPATH: path.join(repository, 'src') }, stdio: ['ignore', 'pipe', 'pipe'],
    });
    let errors = ''; child.stderr.on('data', value => { errors += String(value); });
    try {
      const server = await new Promise<Server>((resolve, reject) => {
        let output = ''; const timer = setTimeout(() => reject(new Error(errors || 'Trace fixture did not start')), 20000);
        child.once('exit', code => { clearTimeout(timer); reject(new Error(`Trace fixture exited ${code}: ${errors}`)); });
        child.stdout.on('data', data => { output += String(data); const line = output.split('\n').find(value => value.startsWith('{"origin"'));
          if (line) { clearTimeout(timer); resolve(JSON.parse(line)); } });
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

function event(seq: number, overrides: Partial<Event> = {}): Event {
  return { id: `trace-${seq}`, seq, kind: 'llm_output', title: `可见记录 ${seq}`, content: `可见内容 ${seq}`,
    created_at: '2026-09-23T04:00:00Z', ...overrides };
}
function attempt(id: string, generation = 1, status = 'running') {
  return { id, generation, status, started_at: '2026-09-23T04:00:00Z', finished_at: null,
    duration_ms: 1234, model: '测试模型' };
}
function pageFor(server: Server, attemptId: string, events: Event[], url: URL, complete = false) {
  const before = url.searchParams.get('before'); const after = url.searchParams.get('after');
  const matches = events.filter(value => before !== null ? value.seq < Number(before) : after !== null ? value.seq > Number(after) : true);
  const items = after !== null ? matches.slice(0, 50) : matches.slice(-50);
  const first = items[0]?.seq; const last = items.at(-1)?.seq;
  return { attempt_id: attemptId, run_id: server.run, work_item_id: server.item, items,
    next_after: last ?? Number(after ?? 0), next_before: first ?? null,
    has_more_before: first != null && events.some(value => value.seq < first),
    has_more_after: last != null && events.some(value => value.seq > last), complete };
}

async function traceFixture(page: Playwright.Page, server: Server, events: Record<string, Event[]>, options?: {
  current?: string; attempts?: ReturnType<typeof attempt>[];
}) {
  const state = { current: options?.current ?? 'current-attempt', attempts: options?.attempts ?? [attempt('current-attempt')],
    events, requests: [] as URL[], listRequests: 0 };
  await page.route(`**/work_items/${server.item}/attempts**`, async route => {
    state.listRequests += 1;
    expect(route.request().method()).toBe('GET'); expect(Boolean(route.request().headers().authorization)).toBeTruthy();
    await route.fulfill({ json: { run_id: server.run, work_item_id: server.item, current_attempt_id: state.current,
      items: state.attempts, next_before: null } });
  });
  await page.route('**/api/v1/attempts/*/trace**', async route => {
    const url = new URL(route.request().url()); state.requests.push(url);
    expect(url.origin).toBe(server.origin); expect(url.searchParams.get('limit')).toBe('50');
    expect(route.request().method()).toBe('GET'); expect(Boolean(route.request().headers().authorization)).toBeTruthy();
    const id = url.pathname.split('/').at(-2)!;
    await route.fulfill({ json: pageFor(server, id, state.events[id] ?? [], url,
      state.attempts.find(value => value.id === id)?.status !== 'running') });
  });
  return state;
}

async function enterTask(page: Playwright.Page, server: Server) {
  await page.goto(`${server.origin}/#bootstrap=${encodeURIComponent(server.bootstrap)}`);
  await expect(page.getByRole('heading', { name: '创建产品', exact: true })).toBeVisible();
  await page.getByRole('button', { name: '执行台', exact: true }).click();
  await page.getByTestId('stage-code_review').click();
  const task = page.locator('.workflow-map-task').first();
  await task.locator(':scope > summary').click();
  return task;
}

test('real trace endpoints deliver incremental redacted content from the fixture store', async ({ page, server }, testInfo) => {
  async function emit(mode: string) {
    const response = await fetch(server.origin + '/__fixture/trace', { method: 'POST', headers: {
      'X-Fixture-Key': server.fixture_key, Origin: server.origin, 'Idempotency-Key': crypto.randomUUID(),
      'Content-Type': 'application/json',
    }, body: JSON.stringify({ mode }) });
    expect(response.ok).toBeTruthy();
  }
  await emit('initial');
  const task = await enterTask(page, server); await task.locator('.task-trace > summary').click();
  const panel = task.locator('.task-trace-panel');
  await expect(panel.locator('.task-trace-event')).toHaveCount(2);
  await expect(panel).toContainText('真实接口任务指令');
  await emit('append');
  await expect(panel.locator('.task-trace-event')).toHaveCount(4);
  await expect(panel).toContainText('真实接口工具返回');
  for (const summary of await panel.locator('.task-trace-event > summary').all()) await summary.click();
  await expect(panel.locator('.task-trace-event-body').filter({ hasText: '[已隐藏]' })).toHaveCount(2);
  await expect(panel).toContainText('发现一项需要核对的权限边界');
  await expect(panel).not.toContainText('fixture-sensitive-value');
  await expect(panel).not.toContainText('fixture-sensitive-token');
  await expect(panel).not.toContainText('PRIVATE_HIDDEN_REASONING');
  await page.screenshot({ path: testInfo.outputPath('task-trace-desktop.png'), fullPage: true });
});

test('task trace loads on demand, receives visible increments and renders raw text safely', async ({ page, server }) => {
  const records = [event(1, { kind: 'instruction', title: '系统与任务指令', content: '按当前代码版本审查权限边界。' }),
    event(2, { kind: 'llm_request', title: '发给模型的请求', content: '{"Authorization":"[已隐藏]","task":"检查权限"}', redacted: true }),
    event(3, { kind: 'llm_output', title: '模型的可见回复', content: '<script>window.__traceExecuted=true</script>' }),
    event(4, { kind: 'tool_call', title: '读取相关文件', content: '{"path":"src/auth.ts"}' })];
  const state = await traceFixture(page, server, { 'current-attempt': records });
  const task = await enterTask(page, server);
  expect(state.requests).toHaveLength(0); expect(state.listRequests).toBe(0);
  await task.locator('.task-trace > summary').click();
  const panel = task.locator('.task-trace-panel');
  await expect(panel.locator('.task-trace-event')).toHaveCount(4);
  await expect(panel.locator('pre')).toHaveCount(0);
  await expect(panel).toContainText('耗时 1.2 秒');
  records.push(event(5, { kind: 'tool_result', title: '文件读取结果', content: '已读取文件的可见内容。', duration_ms: 24 }));
  await expect(panel.locator('.task-trace-event')).toHaveCount(5);
  expect(state.requests.some(url => url.searchParams.get('after') === '4')).toBeTruthy();
  await panel.locator('[data-trace-seq="2"] > summary').click();
  await expect(panel).toContainText('敏感信息已隐藏');
  await expect(panel.locator('pre')).toContainText('[已隐藏]');
  await panel.locator('[data-trace-seq="3"] > summary').click();
  await expect(panel.locator('[data-trace-seq="3"] pre')).toHaveText('<script>window.__traceExecuted=true</script>');
  expect(await page.evaluate(() => (window as any).__traceExecuted)).toBeUndefined();
  await expect(panel.getByRole('button', { name: '跟随最新记录', exact: true })).toBeVisible();
});

test('the waiting seq zero hint is replaced by the first real trace event', async ({ page, server }) => {
  const records = [event(0, { id: 'waiting', kind: 'status', title: '执行记录', content: '执行刚开始，正在等待首条日志。' })];
  const state = await traceFixture(page, server, { 'current-attempt': records });
  const task = await enterTask(page, server); await task.locator('.task-trace > summary').click();
  const panel = task.locator('.task-trace-panel');
  await expect(panel).toContainText('正在等待首条日志');
  await expect(panel.locator('.task-trace-event')).toHaveCount(0);
  records.push(event(1, { kind: 'instruction', title: '首条任务指令' }));
  await expect(panel).toContainText('首条任务指令');
  await expect(panel).not.toContainText('正在等待首条日志');
  expect(state.requests.some(url => url.searchParams.get('after') === '0')).toBeTruthy();
});

test('the current completed attempt still receives delayed analysis and follows a fresh attempt', async ({ page, server }) => {
  const records = [event(1, { kind: 'error', title: '本次执行中断' })];
  const state = await traceFixture(page, server, { 'current-attempt': records,
    'next-attempt': [event(1, { kind: 'instruction', title: '自动重试的新任务指令' })] },
  { attempts: [attempt('current-attempt', 1, 'failed')] });
  const task = await enterTask(page, server); await task.locator('.task-trace > summary').click();
  const panel = task.locator('.task-trace-panel');
  await expect(panel).toContainText('本次执行中断');
  records.push(event(2, { kind: 'error', title: '失败原因分析' }));
  await expect(panel).toContainText('失败原因分析');
  expect(state.requests.some(url => url.searchParams.get('after') === '1')).toBeTruthy();
  state.attempts.unshift(attempt('next-attempt', 2)); state.current = 'next-attempt';
  await expect(panel).toContainText('自动重试的新任务指令');
  await expect(panel).not.toContainText('本次执行中断');
});

test('older records use cursors and a selected historical attempt never jumps to a new attempt', async ({ page, server }) => {
  const state = await traceFixture(page, server, {
    'current-attempt': Array.from({ length: 60 }, (_, index) => event(index + 1)),
    'old-attempt': [event(1, { title: '上一轮执行记录' })],
    'new-attempt': [event(1, { title: '新一轮执行记录' })],
  }, { attempts: [attempt('current-attempt', 2), attempt('old-attempt', 1, 'failed')] });
  const task = await enterTask(page, server); await task.locator('.task-trace > summary').click();
  const panel = task.locator('.task-trace-panel');
  await expect(panel.locator('.task-trace-event')).toHaveCount(50);
  await panel.getByRole('button', { name: '查看更早记录', exact: true }).click();
  await expect(panel.locator('.task-trace-event')).toHaveCount(60);
  expect(state.requests.some(url => url.searchParams.get('before') === '11')).toBeTruthy();
  await panel.getByLabel('查看哪次执行').selectOption('old-attempt');
  await expect(panel).toContainText('上一轮执行记录');
  state.current = 'new-attempt'; state.attempts.unshift(attempt('new-attempt', 3));
  await expect(panel.getByLabel('查看哪次执行').locator('option[value="new-attempt"]')).toHaveCount(1);
  state.attempts = state.attempts.filter(value => value.id !== 'old-attempt');
  const reads = state.listRequests;
  await expect.poll(() => state.listRequests).toBeGreaterThan(reads);
  await expect(panel.getByLabel('查看哪次执行').locator('option[value="old-attempt"]')).not.toContainText('失败');
  await expect(panel.getByLabel('查看哪次执行')).toHaveValue('old-attempt');
  await expect(panel).toContainText('上一轮执行记录');
  await panel.getByLabel('查看哪次执行').selectOption('');
  await expect(panel).toContainText('新一轮执行记录');
  await expect(panel).not.toContainText('上一轮执行记录');
});

test('attempt history pages use the opaque cursor and can return to the current execution', async ({ page, server }) => {
  await traceFixture(page, server, { 'current-attempt': [event(1, { title: '当前执行内容' })],
    'earlier-attempt': [event(1, { title: '更早执行内容' })] });
  const cursors: (string | null)[] = [];
  await page.route(`**/work_items/${server.item}/attempts**`, async route => {
    const before = new URL(route.request().url()).searchParams.get('before'); cursors.push(before);
    await route.fulfill({ json: { run_id: server.run, work_item_id: server.item, current_attempt_id: 'current-attempt',
      items: before ? [attempt('earlier-attempt', 1, 'failed')] : [attempt('current-attempt', 2)],
      next_before: before ? null : 'opaque-previous-attempt' } });
  });
  const task = await enterTask(page, server); await task.locator('.task-trace > summary').click();
  const panel = task.locator('.task-trace-panel');
  await expect(panel).toContainText('当前执行内容');
  await panel.getByRole('button', { name: '更早的执行', exact: true }).click();
  await expect(panel).toContainText('更早执行内容');
  await expect(panel.getByLabel('查看哪次执行')).toHaveValue('earlier-attempt');
  expect(cursors).toContain('opaque-previous-attempt');
  await panel.getByRole('button', { name: '最近的执行', exact: true }).click();
  await expect(panel).toContainText('当前执行内容');
});

test('a late response from an old attempt cannot mix with the newly selected attempt', async ({ page, server }) => {
  await traceFixture(page, server, { 'old-attempt': [event(1, { title: '当前选中的历史执行' })] },
    { attempts: [attempt('current-attempt', 2), attempt('old-attempt', 1, 'completed')] });
  let release!: () => void; let received = false;
  const held = new Promise<void>(resolve => { release = resolve; });
  await page.route('**/api/v1/attempts/current-attempt/trace**', async route => {
    received = true; await held;
    await route.fulfill({ json: pageFor(server, 'current-attempt', [event(1, { title: '不应混入的迟到响应' })], new URL(route.request().url())) }).catch(() => undefined);
  });
  const task = await enterTask(page, server); await task.locator('.task-trace > summary').click();
  const panel = task.locator('.task-trace-panel');
  try {
    await expect.poll(() => received).toBeTruthy();
    await panel.getByLabel('查看哪次执行').selectOption('old-attempt');
    await expect(panel).toContainText('当前选中的历史执行');
    release();
    await expect(panel).not.toContainText('不应混入的迟到响应');
  } finally { release(); }
});

test('large logs stay within a bounded window and older or newer ranges remain reachable', async ({ page, server }) => {
  const records = Array.from({ length: 50 }, (_, index) => event(index + 1, { content: 'x'.repeat(16 * 1024) }));
  const state = await traceFixture(page, server, { 'current-attempt': records });
  const task = await enterTask(page, server); await task.locator('.task-trace > summary').click();
  const panel = task.locator('.task-trace-panel');
  await expect(panel.locator('.task-trace-event')).toHaveCount(32);
  await expect(panel.locator('pre')).toHaveCount(0);
  await expect(panel).toContainText('当前只展示一段记录');
  await panel.getByRole('button', { name: '查看更早记录', exact: true }).click();
  await expect(panel.locator('[data-trace-seq="1"]')).toHaveCount(1);
  await expect(panel.locator('.task-trace-event')).toHaveCount(32);
  await panel.getByRole('button', { name: '读取后续记录', exact: true }).click();
  await expect(panel.locator('[data-trace-seq="50"]')).toHaveCount(1);
  await expect(panel.locator('.task-trace-event')).toHaveCount(32);
  expect(state.requests.some(url => url.searchParams.get('before') === '19')).toBeTruthy();
  expect(state.requests.some(url => url.searchParams.get('after') === '32')).toBeTruthy();
});

for (const failure of ['identity', 'hidden_kind', 'oversized'] as const) {
  test(`invalid trace ${failure} never becomes rendered process content`, async ({ page, server }) => {
    await traceFixture(page, server, {});
    await page.route('**/api/v1/attempts/current-attempt/trace**', async route => {
      const result = pageFor(server, 'current-attempt', [event(1, { title: 'SHOULD_NOT_DISPLAY',
        ...(failure === 'hidden_kind' ? { kind: 'reasoning' } : {}),
        ...(failure === 'oversized' ? { content: 'x'.repeat(1024 * 1024 + 1) } : {}) })], new URL(route.request().url()));
      if (failure === 'identity') result.work_item_id = 'another-work-item';
      await route.fulfill({ json: result });
    });
    const task = await enterTask(page, server); await task.locator('.task-trace > summary').click();
    const panel = task.locator('.task-trace-panel');
    await expect(panel.getByRole('alert')).toContainText(failure === 'oversized' ? '本次过程记录过大' : '过程记录暂时无法核对');
    await expect(panel).not.toContainText('SHOULD_NOT_DISPLAY');
    await expect(panel.locator('.task-trace-event')).toHaveCount(0);
  });
}

test('collapsing process details stops polling and the panel fits a narrow viewport', async ({ page, server }, testInfo) => {
  await page.setViewportSize({ width: 390, height: 844 });
  const state = await traceFixture(page, server, { 'current-attempt': [event(1, { kind: 'instruction', title: '任务指令' }),
    event(2, { kind: 'tool_result', title: '工具返回结果', content: '很长的可见结果'.repeat(500) })] });
  const task = await enterTask(page, server); await task.locator('.task-trace > summary').click();
  await expect(task.locator('.task-trace-event')).toHaveCount(2);
  await task.locator('.task-trace > summary').click();
  await page.waitForTimeout(50);
  const closed = { trace: state.requests.length, attempts: state.listRequests };
  await page.waitForTimeout(2200);
  expect({ trace: state.requests.length, attempts: state.listRequests }).toEqual(closed);
  await task.locator('.task-trace > summary').click();
  await expect.poll(() => state.requests.length).toBeGreaterThan(closed.trace);
  await task.locator(':scope > summary').click();
  await page.waitForTimeout(50);
  const hidden = { trace: state.requests.length, attempts: state.listRequests };
  await page.waitForTimeout(2200);
  expect({ trace: state.requests.length, attempts: state.listRequests }).toEqual(hidden);
  await task.locator(':scope > summary').click();
  await task.locator('[data-trace-seq="2"] > summary').click();
  await expect(task.locator('[data-trace-seq="2"] pre')).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth + 1)).toBeTruthy();
  await page.screenshot({ path: testInfo.outputPath('task-trace-mobile.png'), fullPage: true });
});
