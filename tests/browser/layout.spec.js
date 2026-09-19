const { test, expect } = require('@playwright/test');
test.use({ serviceWorkers: 'block' });
test.afterEach(async ({ page }) => page.unrouteAll({ behavior: 'wait' }));

async function fixture(page, request, baseURL, overrides = {}) {
  const state = await (await request.get(new URL('api/state', baseURL).href)).json();
  Object.assign(state, {
    host: 'workstation', platform: 'linux', mode: 'developer',
    gamewatch: { installed: true, on: false, running: false },
    apps: [{ name: 'T3 Code', url: '/' }, { name: 'Terminal', url: '/term/' }, { name: 'Network', url: '/net/' }],
    tmux: [], agent_history: [], repos: [], inbox: [], usage: null,
    github: { prs: [], error: null }, ci: { rows: [] }, linear: { configured: false, issues: [] },
    scheduled: { available: true, jobs: [], runs: [] },
    services: [{ unit: 't3code', active: 'active', sub: 'running' }],
    docker: { containers: [{ name: 'core-postgres', state: 'running', status: 'healthy' }] },
    journal: [], ...overrides,
  });
  state.settings = { ...state.settings, hidden_panels: [], hidden_apps: [], poll_ms: 30000, custom_apps: [], ...overrides.settings };
  const source = await (await request.get(baseURL)).text();
  const shell = () => source.replace(/const BOOT=[^\n]+/, () => `const BOOT=${JSON.stringify(state).replaceAll('<','\\u003c')};`);
  const benchmark = { state: 'idle', latest: null };
  await page.route('**/api/state', route => route.fulfill({ json: state }));
  await page.route('**/api/t3-activity', route => route.fulfill({ json: { status: 'ok', threads: [] } }));
  await page.route('**/api/admin/releases', route => route.fulfill({ json: { current: 'v0.4.0', releases: [], available: false } }));
  await page.route('**/api/admin/benchmark', route => route.fulfill({ json: benchmark }));
  await page.route('**/*', route => route.request().resourceType() === 'document'
    ? route.fulfill({ contentType: 'text/html', body: shell() }) : route.fallback());
  await page.goto(baseURL);
  await expect(page.locator('#summary-tmux')).toContainText('terminal sessions');
  return { state, benchmark };
}

const toggle = (page, id) => page.locator(`[data-panel="${id}"] .panel-toggle`);
const system = page => page.locator('[data-tab=system]').click();

test('compact defaults keep navigation and launchers within reach', async ({ page, request, baseURL }) => {
  await fixture(page, request, baseURL);
  await expect(page.locator('#gear')).toHaveCount(0);
  await expect(page.locator('#apps a:visible')).toHaveText(['T3 Code', 'Terminal']);
  await expect(page.locator('[data-tab=system]')).toBeInViewport();
  await expect(page.locator('#detail-tmux')).toBeHidden();
  await expect(page.locator('#detail-ci')).toBeHidden();
  await page.locator('#apps-more').click();
  await expect(page.locator('#apps')).toContainText('Network');
  await expect(page.locator('#apps a:visible')).toHaveCount(4); // Includes built-in Files.
  await system(page);
  await expect(page.locator('#launcher')).toBeHidden();
  await expect(page.locator('#detail-stats')).toBeHidden();
  await expect(page.locator('#summary-stats')).toContainText('CPU');
  await expect(page.locator('#summary-benchmark')).toHaveText('Not run yet');
  await expect(page.locator('#detail-benchmark')).toBeHidden();
  await expect(page.locator('#configuration')).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
});

test('expansion and the scroll position survive refreshed data and reload', async ({ page, request, baseURL }) => {
  const { state } = await fixture(page, request, baseURL);
  await system(page);
  await toggle(page, 'services').click();
  await page.locator('[data-settings=dashboard]').scrollIntoViewIfNeeded();
  const before = await page.evaluate(() => scrollY);
  state.services.push({ unit: 'failed-worker', active: 'failed', sub: 'failed' });
  await page.evaluate(s => render(s), state);
  await expect(toggle(page, 'services')).toHaveAttribute('aria-expanded', 'true');
  await expect(page.locator('#summary-services')).toContainText('1 failed');
  expect(await page.evaluate(() => scrollY)).toBe(before);
  await page.reload();
  await expect(toggle(page, 'services')).toHaveAttribute('aria-expanded', 'true');
  await expect(toggle(page, 'benchmark')).toHaveAttribute('aria-expanded', 'false');
  await toggle(page, 'services').click();
  await page.evaluate(s => render(s), state);
  await expect(page.locator('#summary-services')).toContainText('1 failed');
  await expect(page.locator('#detail-services')).toBeHidden();
});

test('active work previews three sessions, then shows all without losing expansion on poll', async ({ page, request, baseURL }) => {
  const tmux = Array.from({ length: 6 }, (_, i) => ({ name: `agent-${i}`, windows: 1, attached: 'detached' }));
  const { state } = await fixture(page, request, baseURL, { tmux });
  await expect(page.locator('#tmux tr:visible')).toHaveCount(3);
  await page.locator('[data-panel=tmux] .panel-more').click();
  await expect(page.locator('#tmux tr:visible')).toHaveCount(6);
  await page.evaluate(s => render(s), state);
  await expect(page.locator('#tmux tr:visible')).toHaveCount(6);
  await toggle(page, 'tmux').click();
  await expect(page.locator('#detail-tmux')).toBeHidden();
  await expect(page.locator('#summary-tmux')).toContainText('6 terminal sessions');
});

test('attention opens a hidden failed section without persisting widget visibility', async ({ page, request, baseURL }) => {
  await fixture(page, request, baseURL, {
    services: [{ unit: 't3code', active: 'failed', sub: 'failed' }], settings: { hidden_panels: ['services'] },
  });
  await page.locator('[data-attention=services]').click();
  await expect(page.locator('[data-tab=system]')).toHaveAttribute('aria-current', 'page');
  await expect(page.locator('#detail-services')).toBeVisible();
  await expect(toggle(page, 'services')).toBeFocused();
  expect(await page.evaluate(() => cfg.hidden_panels)).toContain('services');
  await page.evaluate(()=>poll());
  await expect(page.locator('#detail-services')).toBeVisible();
});

test('configuration has one focused view, a save scope, and a working Back action', async ({ page, request, baseURL }) => {
  const { state } = await fixture(page, request, baseURL);
  const writes = [];
  await page.route('**/api/settings', route => {
    writes.push(route.request().postDataJSON());
    return route.fulfill({ json: { ok: true, settings: writes.at(-1), linear_configured: false } });
  });
  await system(page);
  await page.locator('[data-settings=dashboard]').click();
  await expect(page.locator('#settings-title')).toContainText('Dashboard preferences');
  await expect(page.locator('#settings-scope')).toContainText('shared on this grave');
  await expect(page.locator('#set-linear')).toBeHidden();
  await expect(page.locator('#throttle-row')).toBeHidden();
  await page.locator('#set-poll').selectOption('10000');
  await page.evaluate(s => render(s), { ...state, settings: { ...state.settings, poll_ms: 2000 } });
  await expect(page.locator('#set-poll')).toHaveValue('10000');
  await page.locator('#save-set').click();
  await expect(page.locator('#set-msg')).toContainText('saved');
  expect(writes[0].poll_ms).toBe(10000);
  await page.locator('#settings-x').focus();
  await page.keyboard.press('Shift+Tab');
  await expect(page.locator('#close-set')).toBeFocused();
  await page.keyboard.press('Tab');
  await expect(page.locator('#settings-x')).toBeFocused();
  await page.keyboard.press('Escape');
  await expect(page.locator('#settings-panel')).toBeHidden();
  await expect(page.locator('[data-settings=dashboard]')).toBeFocused();
});

test('a running benchmark stays visible and stoppable with its results collapsed', async ({ page, request, baseURL }) => {
  const { benchmark } = await fixture(page, request, baseURL);
  Object.assign(benchmark, { state: 'running', progress: 60, message: 'Parallel workload · 2 workers' });
  await system(page);
  await expect(page.locator('#bench-progress')).toBeVisible();
  await expect(page.locator('#bench-cancel')).toBeVisible();
  await expect(page.locator('#detail-benchmark')).toBeHidden();
  await page.route('**/api/admin/benchmark', async route => {
    expect(route.request().postDataJSON()).toEqual({ action: 'cancel' });
    await route.fulfill({ json: { state: 'cancelled', message: 'Benchmark cancelled' } });
  });
  await page.locator('#bench-cancel').click();
  await expect(page.locator('#bench-state')).toHaveText('Benchmark cancelled');
  await expect(page.locator('#bench-cancel')).toBeHidden();
});

test('portable preferences remain reachable and contain no machine controls', async ({ page, request, baseURL }) => {
  await fixture(page, request, baseURL, { platform: 'container' });
  await system(page);
  await expect(page.locator('[data-tab=system]')).toHaveText('Preferences');
  await expect(page.locator('[data-settings=machine]')).toBeHidden();
  await expect(page.locator('[data-settings=notifications]')).toBeHidden();
  await expect(page.locator('[data-panel=benchmark]')).toBeHidden();
  await page.locator('[data-settings=dashboard]').click();
  await expect(page.locator('#set-poll')).toBeVisible();
});

test('malformed expansion storage cannot prevent startup', async ({ page, request, baseURL }) => {
  await page.addInitScript(() => localStorage.setItem('grave-panels-v2:/', '{invalid'));
  const errors = [];
  page.on('pageerror', e => errors.push(e.message));
  await fixture(page, request, baseURL);
  await system(page);
  await toggle(page, 'benchmark').click();
  await expect(page.locator('#bench-mode')).toBeVisible();
  expect(errors).toEqual([]);
});


test('gaming mode keeps machine configuration reachable', async ({ page, request, baseURL }) => {
  await fixture(page, request, baseURL, { mode: 'gaming', gamewatch: { installed: true, on: true, running: true } });
  await system(page);
  await page.locator('[data-settings=machine]').click();
  await expect(page.locator('#throttle-row')).toBeVisible();
  await expect(page.locator('#boot-mode-row')).toBeVisible();
  await expect(page.locator('#save-set')).toBeHidden();
});


test('expanded overnight output stays open when the report refreshes', async ({ page, request, baseURL }) => {
  const { state } = await fixture(page, request, baseURL, { scheduled: { available: true, jobs: [], runs: [
    { name: 'checks', status: 'failed', started: 1800000000, exit_code: 1, tail: 'First output' },
  ] } });
  await toggle(page, 'scheduled').click();
  await page.locator('#scheduled summary').click();
  state.scheduled.runs[0].tail='Updated output';
  await page.evaluate(s=>render(s),state);
  await expect(page.locator('#scheduled pre')).toBeVisible();
  await expect(page.locator('#scheduled pre')).toHaveText('Updated output');
});
