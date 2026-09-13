const { test, expect } = require('@playwright/test');
test.use({ serviceWorkers: 'block' });

const publicKeys = ['access', 'host', 'mode', 'now', 'platform', 'resources', 'viewer'];

test('denied viewers receive only telemetry and can read it on a phone', async ({ page, context }) => {
  await context.setExtraHTTPHeaders({ 'Tailscale-User-Login': 'denied@example.test' });
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  await page.goto('/');
  await expect(page.getByRole('heading', { name: 'Read-only status' })).toBeVisible();
  await expect(page.locator('#public-host')).not.toBeEmpty();
  await expect(page.locator('#public-resources')).toContainText('Memory');
  for (const selector of ['#gear', '#apps', '#graveyard-toggle', '.tabs']) {
    await expect(page.locator(selector)).toBeHidden();
  }
  const state = await page.evaluate(async () => (await fetch('api/state')).json());
  expect(Object.keys(state).sort()).toEqual(publicKeys);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await page.evaluate(() => poll());
  expect(errors).toEqual([]);
});

test('an existing owner page changes to read-only when access changes', async ({ page, context }) => {
  await page.goto('/');
  await expect(page.locator('#plot-context')).toBeVisible();
  await page.locator('#gear').click();
  await expect(page.locator('#settings-panel')).toBeVisible();
  await context.setExtraHTTPHeaders({ 'Tailscale-User-Login': 'denied@example.test' });
  await page.evaluate(() => poll());
  await expect(page.locator('#public-status')).toBeVisible();
  await expect(page.locator('#gear')).toBeHidden();
  await expect(page.locator('#settings-panel')).toBeHidden();
  expect(await page.evaluate(() => document.body.style.overflow)).toBe('');
  await context.setExtraHTTPHeaders({ 'Tailscale-User-Login': 'browser@example.test' });
  await page.evaluate(() => poll());
  await expect(page.locator('#public-status')).toBeHidden();
  await expect(page.locator('#gear')).toBeVisible();
  await expect(page.locator('#plot-context')).toBeVisible();
});
