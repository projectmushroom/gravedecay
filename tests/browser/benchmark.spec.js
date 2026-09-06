const { test, expect } = require('@playwright/test');
test.use({ serviceWorkers: 'block' });

test.beforeEach(async ({ page }) => {
  // CI has no t3code unit; retain the developer UI through polls and reloads.
  await page.addInitScript(() => addEventListener('DOMContentLoaded', () => {
    const normalize = () => {
      if (document.body.classList.contains('gaming')) document.body.classList.remove('gaming');
    };
    new MutationObserver(normalize).observe(document.body, { attributes: true, attributeFilter: ['class'] });
    normalize();
  }));
});

const result = {
  suite: 'grave-dev-v1', mode: 'capacity', score: 250, measured_at: 1788696000,
  environment: { host: 'development-mac', os: 'Darwin', architecture: 'arm64', python: '3.13.1', git: 'git version 2.50', execution: 'macos' },
  single: { total_s: 4, repository_s: 1, build_s: 2, tests_s: 1 },
  parallel: [{ workers: 1, cycles_per_min: 15, slowdown: 1 }, { workers: 2, cycles_per_min: 25, slowdown: 1.2 }],
  best_workers: 2,
};

test('benchmark runs, reconnects, cancels and exports on mobile', async ({ page }) => {
  let state = { state: 'idle', latest: result };
  const requests = [];
  await page.route('**/api/admin/benchmark', async route => {
    if (route.request().method() === 'POST') {
      const data = route.request().postDataJSON();
      requests.push(data);
      state = data.action === 'cancel'
        ? { state: 'cancelled', message: 'Benchmark cancelled; previous result kept', latest: result }
        : { state: 'running', message: 'Parallel workload · 2 workers', progress: 60, latest: result };
    }
    await route.fulfill({ json: state });
  });
  await page.goto('./?tab=system');
  await expect(page.locator('#bench-run')).toBeEnabled();
  await page.locator('#bench-mode').selectOption('capacity');
  await page.locator('#bench-run').click();
  await expect(page.locator('#bench-progress')).toBeVisible();
  await expect(page.locator('#bench-run')).toBeDisabled();
  expect(requests).toEqual([{ action: 'start', mode: 'capacity' }]);
  await page.reload();
  await expect(page.locator('#bench-cancel')).toBeVisible();
  await expect(page.locator('#bench-state')).toContainText('Parallel workload');
  await page.locator('#bench-cancel').click();
  await expect(page.locator('#bench-state')).toContainText('cancelled');
  await expect(page.locator('#bench-run')).toBeEnabled();
  await expect(page.locator('#bench-result')).toContainText('250');
  const cells = await page.locator('[aria-label="Benchmark timings"] td').evaluateAll(cells =>
    cells.map(cell => ({ x: cell.getBoundingClientRect().x, y: cell.getBoundingClientRect().y })));
  expect(cells[1].x).toBeGreaterThan(cells[0].x);
  expect(Math.abs(cells[1].y - cells[0].y)).toBeLessThan(1);
  const download = page.waitForEvent('download');
  await page.locator('#bench-export').click();
  expect((await download).suggestedFilename()).toContain('grave-dev-v1');
  const dimensions = await page.evaluate(() => ({ width: document.documentElement.clientWidth, scroll: document.documentElement.scrollWidth }));
  expect(dimensions.scroll).toBeLessThanOrEqual(dimensions.width);
});

test('benchmark permission failure leaves run disabled', async ({ page }) => {
  await page.route('**/api/admin/benchmark', route => route.fulfill({ status: 403, json: { output: 'Owner access required' } }));
  await page.goto('./?tab=system');
  await expect(page.locator('#bench-state')).toContainText('Owner access required');
  await expect(page.locator('#bench-run')).toBeDisabled();
});
