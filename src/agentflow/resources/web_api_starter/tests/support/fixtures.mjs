import {test as base, expect} from '@playwright/test';

export const test = base.extend({
  baseURL: async ({}, use) => {
    const value = process.env.AGENTFLOW_TEST_BASE_URL;
    if (!value || !value.startsWith('http://127.0.0.1:')) throw new Error('Frozen product startup did not provide its test URL');
    await use(value);
  },
});
export {expect};
