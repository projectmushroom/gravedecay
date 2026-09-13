// Exercise the actual browsers compose service, not just its open TCP port.
const assert = require('node:assert/strict');
const { chromium } = require('@playwright/test');

(async () => {
  let browser;
  try {
    browser = await chromium.connect('ws://127.0.0.1:3050/', { timeout: 30_000 });
    const page = await browser.newPage();
    await page.setContent('<title>grave browser smoke</title><main>browser ready</main>');
    assert.equal(await page.title(), 'grave browser smoke');
    assert.equal(await page.locator('main').textContent(), 'browser ready');
    console.log(`Playwright service launched Chromium ${browser.version()}`);
  } finally {
    await browser?.close();
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
