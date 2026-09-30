// Actual compiled owner UI and actual Product API; no intercepted/mocked requests.
import assert from 'node:assert/strict';
import {createRequire} from 'node:module';
import {writeFile} from 'node:fs/promises';
import path from 'node:path';
import {fileURLToPath} from 'node:url';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../..');
const {chromium} = createRequire(import.meta.url)(path.join(root, 'apps/dashboard/node_modules/@playwright/test/index.js'));
let input = '';
for await (const chunk of process.stdin) input += chunk;
const request = JSON.parse(input);
const browser = await chromium.launch({headless: true});
const context = await browser.newContext({viewport: {width: 1440, height: 1100}});
const page = await context.newPage();
const errors = [];
let productId, postCount = 0, timer;
page.on('pageerror', error => errors.push(error.message));
page.on('request', value => {
  if (value.method() === 'POST' && new URL(value.url()).pathname === '/api/v1/products') postCount += 1;
});
let complete, fail;
const terminal = new Promise((resolve, reject) => { complete = resolve; fail = reject; });
page.on('response', async response => {
  if (!productId || response.request().method() !== 'GET'
      || new URL(response.url()).pathname !== '/api/v1/products/' + productId) return;
  try {
    assert.equal(response.status(), 200);
    const product = await response.json();
    if (['completed', 'blocked', 'cancelled'].includes(product.state)) complete(product);
  } catch (error) { fail(error); }
});
try {
  await page.goto(request.bootstrap_url);
  const form = page.getByRole('form', {name: '创建产品', exact: true});
  await form.getByLabel('产品名称', {exact: true}).fill(request.name);
  await form.getByLabel('产品目标', {exact: true}).fill(request.goal);
  await form.getByRole('checkbox', {name: 'Web 应用', exact: true}).check();
  await form.getByRole('checkbox', {name: 'API 服务', exact: true}).check();
  assert(await form.getByRole('checkbox', {name: 'Web 应用', exact: true}).isChecked());
  assert(await form.getByRole('checkbox', {name: 'API 服务', exact: true}).isChecked());
  await form.getByLabel('输出目录（可选）', {exact: true}).fill(request.output_directory);
  await form.getByLabel('人工参与方式', {exact: true}).selectOption('auto');
  const createdResponse = page.waitForResponse(response => response.request().method() === 'POST'
    && new URL(response.url()).pathname === '/api/v1/products');
  await form.getByRole('button', {name: '开始创建产品', exact: true}).click();
  const response = await createdResponse;
  assert.equal(response.status(), 202, await response.text());
  const submitted = response.request().postDataJSON();
  assert.equal(submitted.goal, request.goal);
  assert.equal(submitted.output_directory, request.output_directory);
  assert.deepEqual(submitted.targets, ['web', 'api']);
  assert.equal(submitted.review_mode, 'auto');
  assert(response.request().headers()['idempotency-key']);
  const created = await response.json(); productId = created.id;
  await page.getByRole('heading', {name: request.name, exact: true}).waitFor();
  await page.screenshot({path: path.join(request.evidence_directory, 'owner-ui-submitted.png'), fullPage: true});
  timer = setTimeout(() => fail(new Error('Actual owner UI did not observe product completion within 900 seconds')), 900_000);
  const product = await terminal;
  if (product.state === 'completed') await page.getByTestId('product-delivery').waitFor();
  await page.screenshot({path: path.join(request.evidence_directory, 'owner-ui-final.png'), fullPage: true});
  assert.deepEqual(errors, []);
  assert.equal(postCount, 1);
  await writeFile(path.join(request.evidence_directory, 'ui-entry.json'), JSON.stringify({entry: 'compiled_webui',
    product_id: productId, product_posts: postCount, response_status: response.status(), mocked_requests: false}, null, 2));
  console.log(JSON.stringify(product));
  if (product.state !== 'completed') process.exitCode = 2;
} finally {
  clearTimeout(timer);
  await context.close();
  await browser.close();
}
