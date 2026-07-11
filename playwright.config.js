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
  projects: [
    { name: 'webkit-iphone-se', use: { ...devices['iPhone SE'] } },
    { name: 'webkit-iphone-13', use: { ...devices['iPhone 13'] } },
    { name: 'webkit-ipad', use: { ...devices['iPad Mini'] } },
    { name: 'chromium-narrow-mac', use: { browserName: 'chromium', viewport: { width: 640, height: 800 } } },
  ],
});
