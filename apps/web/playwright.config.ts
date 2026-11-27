import { defineConfig } from '@playwright/test';

export default defineConfig({
  testDir: './e2e',
  use: {
    channel: process.env.PLAYWRIGHT_CHANNEL || undefined,
    baseURL: 'http://127.0.0.1:4173',
    trace: 'retain-on-failure'
  },
  webServer: {
    command: 'npm run build && node build',
    env: { HOST: '127.0.0.1', PORT: '4173', ORIGIN: 'http://127.0.0.1:4173' },
    timeout: 120_000,
    port: 4173,
    reuseExistingServer: !process.env.CI
  }
});
