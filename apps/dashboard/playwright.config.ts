import { defineConfig, devices } from '@playwright/test';
import path from 'node:path';

// Keep browser lookup stable even when an integration fixture changes cwd.
const browserPath = process.env.PLAYWRIGHT_BROWSERS_PATH ?? '.playwright-browsers';
if (browserPath !== '0') {
  process.env.PLAYWRIGHT_BROWSERS_PATH = path.resolve(import.meta.dirname, browserPath);
}

export default defineConfig({
  testDir: path.resolve(import.meta.dirname, '../../tests/browser'),
  tsconfig: path.resolve(import.meta.dirname, '../../tests/browser/tsconfig.json'),
  outputDir: path.resolve(import.meta.dirname, '../../tests/browser/artifacts'),
  fullyParallel: false, workers: 1, retries: 0, timeout: 40_000,
  reporter: [['list'], ['html', { outputFolder: '../../tests/browser/report', open: 'never' }]],
  use: { ...devices['Desktop Chrome'], viewport: { width: 1440, height: 1000 },
    trace: 'retain-on-failure', screenshot: 'only-on-failure', actionTimeout: 10_000 },
});
