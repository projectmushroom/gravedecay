const { test, expect } = require('@playwright/test');
// Route fixtures must also intercept requests after the PWA claims the page.
test.use({ serviceWorkers: 'block' });

const plot = (id, platform = 'linux', links = { dashboard: '/grave/', t3: '/', terminal: '/term/' }) => ({
  id, dns: `${id}.tail.ts.net`, name: id === 'mac' ? 'My Mac' : 'My VM', lastSeen: Date.now() / 1000,
  summary: { product: 'gravedecay', api_version: 1, node: { host: id, platform, mode: 'developer' },
    resources: {}, activity: { sessions_live: 2 }, health: { services_failed: 0, containers_problem: 0 }, links },
});

test('saved plots survive a reload and an offline selected plot never switches', async ({ page }) => {
  let plots = [plot('mac', 'macos'), plot('vm')];
  await page.route('**/api/graveyard', route => route.fulfill({ json: { state: 'ready', plots } }));
  await page.goto('./');
  await expect(page.locator('.plot-card')).toHaveCount(2);
  await expect(page.locator('[data-plot="vm"] a').first()).toHaveAttribute('href', 'https://vm.tail.ts.net/grave/');
  await page.getByLabel('Plot', { exact: true }).selectOption('mac');
  plots = [plot('vm')];
  await page.reload();
  await expect(page.getByLabel('Plot', { exact: true })).toHaveValue('mac');
  await expect(page.locator('.plot-card')).toHaveCount(1);
  await expect(page.locator('.plot-card')).toContainText('My Mac');
  await expect(page.locator('.plot-card')).toContainText('Unreachable');
  await expect(page.locator('.plot-card a')).toHaveCount(0);
  await page.getByRole('button', { name: 'Forget plot' }).click();
  await expect(page.getByLabel('Plot', { exact: true })).toHaveValue('');
  await expect(page.locator('.plot-card')).toContainText('My VM');
});

test('overview fits phones and only offers published safe links', async ({ page }) => {
  const mac = plot('mac', 'macos', { terminal: '//external.example', dashboard: '/grave/../term' });
  mac.name = 'My very long plot name with no spaces: ' + 'x'.repeat(130);
  await page.route('**/api/graveyard', route => route.fulfill({ json: { state: 'ready', plots: [mac, plot('vm')] } }));
  await page.goto('./');
  await expect(page.locator('.plot-card')).toHaveCount(2);
  await expect(page.locator('[data-plot="mac"] a')).toHaveCount(0);
  const dimensions = await page.evaluate(() => ({ width: document.documentElement.clientWidth, content: document.documentElement.scrollWidth }));
  expect(dimensions.content).toBeLessThanOrEqual(dimensions.width);
});

test('revoked overview access hides remembered inventory', async ({ page }) => {
  let denied = false;
  await page.route('**/api/graveyard', route => denied
    ? route.fulfill({ status: 403, json: { error: 'forbidden' } })
    : route.fulfill({ json: { state: 'ready', plots: [plot('mac')] } }));
  await page.goto('./');
  await expect(page.locator('.plot-card')).toHaveCount(1);
  denied = true;
  await page.getByRole('button', { name: 'Refresh plots' }).click();
  await expect(page.locator('#graveyard-status')).toContainText('Sign in');
  await expect(page.locator('.plot-card')).toHaveCount(0);
});
