const { defineConfig, devices } = require('@playwright/test');

module.exports = defineConfig({
  testDir: './tests/browser',
  timeout: 30_000,
  fullyParallel: true,
  use: {
    baseURL: process.env.GRAVEDECAY_TEST_URL || 'http://127.0.0.1:3000/',
    locale: 'en-US',
    reducedMotion: 'reduce',
  },
  webServer: {
    command: 'GRAVEDECAY_PORT=3000 GRAVE_ROOT=/tmp/grave-root python3 dashboard/gravedecay.py',
    url: 'http://127.0.0.1:3000/healthz',
    reuseExistingServer: true,
  },
  projects: [
    { name: 'webkit-iphone-se', use: { ...devices['iPhone SE'] } },
    { name: 'chromium-narrow-mac', use: { browserName: 'chromium', viewport: { width: 640, height: 800 } } },
  ],
});
