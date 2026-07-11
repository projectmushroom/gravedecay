const { test, expect } = require('@playwright/test');

async function expectNoHorizontalOverflow(page, label) {
  const dimensions = await page.evaluate(() => ({
    viewport: document.documentElement.clientWidth,
    document: document.documentElement.scrollWidth,
    offenders: [...document.querySelectorAll('body *')]
      .filter(el => {
        const rect = el.getBoundingClientRect();
        return rect.right > document.documentElement.clientWidth + 1 || rect.left < -1;
      })
      .slice(0, 8)
      .map(el => `${el.tagName.toLowerCase()}#${el.id}.${el.className}`),
  }));
  expect(dimensions.document, `${label}: ${dimensions.offenders.join(', ')}`).toBeLessThanOrEqual(dimensions.viewport);
}

test.beforeEach(async ({ page }) => {
  await page.goto('./');
  await expect(page.locator('#apps')).not.toBeEmpty();
});

test('work and system dashboards fit the installed-app viewport', async ({ page }) => {
  await expectNoHorizontalOverflow(page, 'work tab');
  await page.locator('[data-tab="system"]').click();
  await expect(page.locator('[data-panel="stats"]')).toBeVisible();
  await expect(page.locator('[data-act="update-grave"]')).toBeVisible();
  await expectNoHorizontalOverflow(page, 'system tab');
});

test('settings and narrow data records remain usable', async ({ page }) => {
  await page.locator('#gear').click();
  await expect(page.locator('#settings-panel')).toBeVisible();
  await expectNoHorizontalOverflow(page, 'settings dialog');
  const close = page.locator('#settings-x');
  const box = await close.boundingBox();
  expect(box.width).toBeGreaterThanOrEqual(32);
  expect(box.height).toBeGreaterThanOrEqual(32);
});

test('PWA contract spans the appliance origin', async ({ request, baseURL }) => {
  const manifest = await (await request.get(new URL('manifest.webmanifest', baseURL).href)).json();
  expect(manifest.scope).toBe('/');
  expect(manifest.id).toBe('/grave/');
  const worker = await request.get(new URL('sw.js', baseURL).href);
  expect(worker.headers()['service-worker-allowed']).toBe('/');
});
