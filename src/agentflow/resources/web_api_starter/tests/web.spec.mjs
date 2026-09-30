import {test} from './support/fixtures.mjs';

test('starter has no goal-specific Web acceptance tests', async ({page}) => {
  await page.goto('/');
  throw new Error('Replace this failing placeholder with accepted user journeys against the implemented UI.');
});
