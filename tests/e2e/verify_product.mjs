// Independent browser acceptance of the delivered fixture product, outside its own test suite.
import assert from 'node:assert/strict';
import {createRequire} from 'node:module';
import path from 'node:path';
import {fileURLToPath} from 'node:url';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../..');
const {chromium} = createRequire(import.meta.url)(path.join(root, 'apps/dashboard/node_modules/@playwright/test/index.js'));
const url = process.argv[2];
const screenshot = process.argv[3];
assert.match(url, /^http:\/\/127\.0\.0\.1:\d+$/);
const browser = await chromium.launch({headless: true});
const context = await browser.newContext();
const page = await context.newPage();
const headers = [];
page.on('request', request => { headers.push(request.headers()); });
try {
  await page.goto(url);
  await page.getByRole('heading', {name: '阅读清单', exact: true}).waitFor();
  await page.getByLabel('书名', {exact: true}).fill('Independent Browser Book');
  await page.getByLabel('作者', {exact: true}).fill('Independent Reader');
  await page.getByRole('button', {name: '添加图书', exact: true}).click();
  await page.getByLabel('已读 Independent Browser Book', {exact: true}).check();
  await page.getByLabel('阅读状态', {exact: true}).selectOption('unread');
  await page.getByText('Independent Browser Book', {exact: true}).waitFor({state: 'hidden'});
  await page.getByLabel('阅读状态', {exact: true}).selectOption('read');
  await page.getByText('Independent Browser Book', {exact: true}).waitFor();
  await page.reload();
  const persisted = page.getByLabel('已读 Independent Browser Book', {exact: true});
  await persisted.waitFor();
  assert(await persisted.isChecked());
  await page.screenshot({path: screenshot, fullPage: true});
  assert(headers.every(value => !value.authorization && !value.cookie));
  console.log(JSON.stringify({browser_verified: true, url, screenshot, owner_credentials_sent: false}));
} finally {
  await context.close();
  await browser.close();
}
