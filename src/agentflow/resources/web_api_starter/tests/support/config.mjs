import path from 'node:path';
import {fileURLToPath} from 'node:url';
import {defineConfig} from '@playwright/test';

const here = path.dirname(fileURLToPath(import.meta.url));
const tests = path.resolve(here, '..');
const reports = path.resolve(tests, '../../reports');
export function configuration(testMatch) {
  return defineConfig({
    testDir: tests, testMatch, workers: 1, retries: 0, timeout: 20_000,
    globalSetup: path.join(here, 'global-setup.mjs'),
    outputDir: path.join(reports, 'playwright'),
    reporter: [['list'], ['json', {outputFile: process.env.PLAYWRIGHT_JSON_OUTPUT_NAME || path.join(reports, testMatch + '.json')}]],
    use: {headless: true, trace: 'retain-on-failure',
      launchOptions: process.env.AGENTFLOW_BROWSER_EXECUTABLE ? {executablePath: process.env.AGENTFLOW_BROWSER_EXECUTABLE} : {}},
  });
}
