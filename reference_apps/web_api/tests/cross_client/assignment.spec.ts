import {test,expect} from '@playwright/test';
test('Android then Linux changes are visible in Web after reload',async({page})=>{
  const title=process.env.AGENTFLOW_CROSS_TICKET_TITLE;
  expect(title,'The coordinator must bind the API-created ticket').toBeTruthy();
  await page.goto('/');await page.getByRole('button',{name:'Sign in',exact:true}).click();
  const row=page.getByRole('listitem').filter({has:page.getByText(title!,{exact:true})});
  await expect(row).toContainText('assigned: manager');
  await page.reload();await expect(row).toContainText('assigned: manager');
});
