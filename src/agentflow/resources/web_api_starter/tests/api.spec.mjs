import {test} from './support/fixtures.mjs';

test('starter has no goal-specific API acceptance tests', async ({request}) => {
  await request.get('/health');
  throw new Error('Replace this failing placeholder with accepted API behavior, validation and persistence cases.');
});
