import {test, expect} from './support/fixtures.mjs';
test('reader can add edit and delete a book', async ({page}) => {
  await page.goto('/'); await expect(page.getByRole('heading', {name: '阅读清单'})).toBeVisible();
  await page.getByLabel('书名', {exact: true}).fill('UI Book'); await page.getByLabel('作者', {exact: true}).fill('UI Writer');
  await page.getByRole('button', {name: '添加图书', exact: true}).click(); await expect(page.getByText('UI Book', {exact: true})).toBeVisible();
  await page.getByRole('button', {name: '编辑 UI Book', exact: true}).click(); await page.getByLabel('书名', {exact: true}).fill('UI Edited');
  await page.getByRole('button', {name: '保存修改', exact: true}).click(); await expect(page.getByText('UI Edited', {exact: true})).toBeVisible();
  await page.getByRole('button', {name: '删除 UI Edited', exact: true}).click(); await expect(page.getByText('UI Edited', {exact: true})).toHaveCount(0);
});
test('read status filters and persists after reload', async ({page}) => {
  await page.goto('/'); await page.getByLabel('书名', {exact: true}).fill('Read State Book'); await page.getByLabel('作者', {exact: true}).fill('Reader');
  await page.getByRole('button', {name: '添加图书', exact: true}).click(); await page.getByLabel('已读 Read State Book', {exact: true}).check();
  await page.getByLabel('阅读状态', {exact: true}).selectOption('unread'); await expect(page.getByText('Read State Book', {exact: true})).toHaveCount(0);
  await page.getByLabel('阅读状态', {exact: true}).selectOption('read'); await expect(page.getByText('Read State Book', {exact: true})).toBeVisible();
  await page.reload(); await expect(page.getByLabel('已读 Read State Book', {exact: true})).toBeChecked();
});
