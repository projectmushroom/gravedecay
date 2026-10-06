// Night Shift M3: one review card per scheduled run (docs/DASHBOARD.md).
const { test, expect } = require('@playwright/test');
test.use({ serviceWorkers: 'block' });
test.afterEach(async ({ page }) => page.unrouteAll({ behavior: 'wait' }));

const runs = [
  { name: 'nightly', repo: 'project', agent: 'codex', status: 'succeeded', exit_code: 0, started: 1800000000, finished: 1800003600,
    session: 'job-nightly-20270115T020000-abc123', log: 'session-20270115.log', tail: 'provider output',
    result: { verdict: 'ready-for-review', changes: { commits: 2, files: 3, insertions: 40, deletions: 5, dirty: false },
      checks: [{ cmd: 'vitest run', exit_code: 0, seconds: 12.5, tail: 'ok', source: 'base:0123456789abcdef0123456789abcdef01234567' }],
      cost: { usd: 0.1234, estimated: true, model: 'gpt-x' }, test_changes: [] } },
  { name: 'weekly', repo: 'project', agent: 'claude', status: 'succeeded', exit_code: 0, started: 1799990000,
    session: 'job-weekly-20270114T020000-def456', log: 'session-20270114.log', tail: '',
    result: { verdict: 'checks-failed', changes: { commits: 1, files: 2, insertions: 3, deletions: 30, dirty: true },
      checks: [{ cmd: '<script>pytest</script>', exit_code: 1, seconds: 3, tail: 'FAILED', source: 'owner' }, { cmd: 'make lint', exit_code: null, seconds: 1, tail: '', source: 'owner' }],
      cost: null, test_changes: ['deleted test tests/test_api.py', 'skip/only/xfail added in tests/test_x.py (+2)'] } },
  { name: 'docs', repo: 'project', agent: 'codex', status: 'succeeded', exit_code: 0, started: 1799980000,
    result: { verdict: 'unverified', changes: { commits: 1, files: 1, insertions: 1, deletions: 0, dirty: false }, checks: [], cost: null } },
  { name: 'broken', repo: 'project', agent: 'codex', status: 'failed', exit_code: 9, started: 1799970000, reason: 'provider exited 9',
    result: { verdict: 'provider-failed', changes: null, checks: [], cost: null } },
  { name: 'quiet', repo: 'project', agent: 'codex', status: 'succeeded', exit_code: 0, started: 1799960000,
    result: { verdict: 'no-changes', changes: { commits: 0, files: 0, insertions: 0, deletions: 0, dirty: false }, checks: [], cost: null } },
];

async function fixture(page, request, baseURL) {
  const state = await (await request.get(new URL('api/state', baseURL).href)).json();
  Object.assign(state, {
    host: 'workstation', platform: 'linux', mode: 'developer',
    gamewatch: { installed: true, on: false, running: false },
    apps: [{ name: 'T3 Code', url: '/' }, { name: 'Terminal', url: '/term/' }],
    tmux: [], agent_history: [], repos: [], inbox: [], usage: null,
    github: { prs: [], error: null }, ci: { rows: [] }, linear: { configured: false, issues: [] },
    scheduled: { available: true, jobs: [{ name: 'nightly', schedule: '02:00', enabled: true, next_due: 1800086400 }], runs },
    services: [{ unit: 't3code', active: 'active', sub: 'running' }],
    docker: { containers: [] }, journal: [],
  });
  state.settings = { ...state.settings, hidden_panels: [], hidden_apps: [], poll_ms: 30000, custom_apps: [] };
  const source = await (await request.get(baseURL)).text();
  await page.route('**/api/state', route => route.fulfill({ json: state }));
  await page.route('**/api/t3-activity', route => route.fulfill({ json: { status: 'ok', threads: [] } }));
  await page.route('**/api/admin/releases', route => route.fulfill({ json: { current: 'v0.4.0', releases: [], available: false } }));
  await page.route('**/api/admin/benchmark', route => route.fulfill({ json: { state: 'idle', latest: null } }));
  await page.route('**/*', route => route.request().resourceType() === 'document'
    ? route.fulfill({ contentType: 'text/html', body: source.replace(/const BOOT=[^\n]+/, () => `const BOOT=${JSON.stringify(state).replaceAll('<', '\\u003c')};`) })
    : route.fallback());
  await page.goto(baseURL);
  await expect(page.locator('#summary-tmux')).toContainText('terminal sessions');
  return state;
}

test('the overnight report shows one review card per run', async ({ page, request, baseURL }) => {
  await fixture(page, request, baseURL);
  // Needs attention counts verdicts, not lifecycle status: checks-failed, provider-failed, unverified.
  await expect(page.locator('#attention')).toContainText('3 overnight runs need attention');
  await expect(page.locator('#summary-scheduled')).toContainText('latest: nightly ready-for-review');
  await page.locator('#attention button[data-attention=scheduled]').click();
  const cards = page.locator('#scheduled .runcard');
  await expect(cards).toHaveCount(5);
  await expect(cards.locator('.verdict')).toHaveText(['ready-for-review', 'checks-failed', 'unverified', 'provider-failed', 'no-changes']);

  const ready = cards.nth(0);
  await expect(ready).toHaveAttribute('data-verdict', 'ready-for-review');
  await expect(ready).toContainText('2 commits · 3 files · +40 −5');
  await expect(ready.locator('.checks li')).toHaveText([/vitest run · base:0123456789ab · exit 0 · 12\.5s/]); // the record keeps the full SHA
  await expect(ready).toContainText('$0.12 estimated · gpt-x');
  await expect(ready.locator('.testchanges')).toHaveCount(0);

  const failed = cards.nth(1);
  await expect(failed).toContainText('uncommitted work left in the worktree');
  await expect(failed.locator('.checks li')).toHaveText([/<script>pytest<\/script> · owner · exit 1 · 3s/, /make lint · owner · stopped · 1s/]);
  await expect(failed.locator('script')).toHaveCount(0);
  await expect(failed.locator('.testchanges')).toHaveText('⚠ Test changes: deleted test tests/test_api.py · skip/only/xfail added in tests/test_x.py (+2)');
  await expect(failed.locator('.testchanges')).toHaveCSS('color', 'rgb(255, 176, 0)');
  await expect(failed).toContainText('cost unknown');

  await expect(cards.nth(2)).toContainText('No checks ran; nothing verified the work.');
  await expect(cards.nth(3)).toContainText('provider exited 9');
  await expect(cards.nth(3)).toContainText('changes unknown');
  await expect(cards.nth(4)).not.toContainText('No checks ran');
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
});

test('the diff link opens the committed work on the dashboard origin', async ({ page, request, baseURL }) => {
  await fixture(page, request, baseURL);
  let requested;
  await page.route('**/api/run-diff*', route => {
    requested = new URL(route.request().url());
    route.fulfill({ contentType: 'text/plain; charset=utf-8', body: 'diff --git a/x b/x\n+<b>added</b>\n' });
  });
  await page.locator('[data-panel=scheduled] .panel-toggle').click();
  const card = page.locator('#scheduled .runcard').nth(1);
  await card.locator('summary').click();
  await expect(card.locator('pre')).toHaveText('No output recorded.');
  await expect(card.getByRole('link', { name: 'Open worktree', exact: true })).toHaveAttribute('href', /arg=job-weekly-20270114T020000-def456$/);
  await card.getByRole('link', { name: 'Diff', exact: true }).click();
  await expect(page.locator('#log-dlg')).toBeVisible();
  await expect(page.locator('#log-text')).toHaveText('diff --git a/x b/x\n+<b>added</b>');
  await expect(page.locator('#log-text b')).toHaveCount(0);
  expect(requested.origin).toBe(new URL(baseURL).origin);
  expect(requested.searchParams.get('name')).toBe('job-weekly-20270114T020000-def456');
  await page.locator('#log-x').click();
  await expect(page.locator('#log-dlg')).toBeHidden();
});
