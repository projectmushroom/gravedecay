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

async function expectPanelsContainContent(page, label) {
  const clipped = await page.evaluate(() => [...document.querySelectorAll('.panel')]
    .filter(panel => getComputedStyle(panel).display !== 'none')
    .flatMap(panel => {
      const panelRect = panel.getBoundingClientRect();
      const overflow = panel.scrollWidth > panel.clientWidth + 1
        ? [`${panel.dataset.panel}: ${panel.clientWidth}/${panel.scrollWidth}`]
        : [];
      const paintedPastEdge = [...panel.querySelectorAll('td, .tile, pre')]
        .filter(el => {
          const rect = el.getBoundingClientRect();
          return rect.right > panelRect.right + 1 || rect.left < panelRect.left - 1;
        })
        .map(el => `${panel.dataset.panel} > ${el.tagName.toLowerCase()}#${el.id}`);
      return overflow.concat(paintedPastEdge);
    }));
  expect(clipped, label).toEqual([]);
}

async function renderLongMobileRecords(page) {
  await page.evaluate(async () => {
    const state = await (await fetch('api/state')).json();
    state.tmux = [
      { name: 'claude-yolo-with-a-deliberately-long-session-name', windows: 12, attached: 'detached',
        worktree: { repo: 'a-deliberately-long-repository-name',
          branch: 'agent/a-deliberately-long-isolated-feature-branch',
          dir: '/srv/dev/worktrees/a-deliberately-long-repository-name/session' } },
    ];
    state.repos = [
      { name: 'wecollect4you-with-a-long-name', branch: 'codex/aggregate-wec-68-prs-95-104', dirty: 1,
        last_when: '3 days ago', last_subject: 'A deliberately long commit subject' },
    ];
    state.usage = {
      claude: {
        today: { in: 0, out: 0, cache: 0, cost: 0, msgs: 0 },
        week: { in: 89000, out: 675000, cache: 100000000, cost: 106.51, msgs: 668 },
      },
      codex: {
        today: { in: 48100000, cached: 47300000, out: 138000, sessions: 5 },
        week: { in: 50900000, cached: 50400000, out: 151000, sessions: 7 },
      },
      codex_limits: {
        plan: 'plus',
        primary: { pct: 46, mins: 300, resets_at: 1783877854 },
        secondary: { pct: 28, mins: 10080, resets_at: 1784409683 },
      },
    };
    render(state);
  });
}

test.beforeEach(async ({ page }) => {
  await page.goto('./');
  await expect(page.locator('#apps')).not.toBeEmpty();
  // Generic CI runners have no active t3code.service and therefore report
  // gaming mode. Keep the visual fixture in developer presentation even when
  // a background poll reapplies the real host mode while a test is running.
  await page.evaluate(() => {
    const normalize = () => {
      if (document.body.classList.contains('gaming')) document.body.classList.remove('gaming');
    };
    new MutationObserver(normalize).observe(document.body, { attributes: true, attributeFilter: ['class'] });
    normalize();
  });
});

test('work and system dashboards fit the installed-app viewport', async ({ page }) => {
  await renderLongMobileRecords(page);
  await expectNoHorizontalOverflow(page, 'work tab');
  await expectPanelsContainContent(page, 'work panels clip their rendered records');
  await page.locator('[data-tab="system"]').click();
  await expect(page.locator('[data-panel="stats"]')).toBeVisible();
  await expect(page.locator('#update-open')).toBeVisible();
  await expectNoHorizontalOverflow(page, 'system tab');
  await expectPanelsContainContent(page, 'system panels clip their rendered records');
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

test('macOS renders its local work tab and repository-root setting', async ({ page }) => {
  await page.evaluate(async () => {
    const state = await (await fetch('api/state')).json();
    state.platform = 'macos';
    state.mode = 'developer';
    state.repos = [{ name: 'owner/a-private-repository', branch: 'main', dirty: 1,
      last_when: 'now', last_subject: 'A private local commit' }];
    state.repo_scan = { root: '/Users/test/Sites', error: null };
    state.github = { error: null, repos: [{ repo: 'owner/a-private-repository',
      url: 'https://github.com/example/repo',
      prs: [{ number: 7, title: 'An open pull request', url: 'https://github.com/example/repo/pull/7' }],
      issues: [{ number: 8, title: 'An open issue', url: 'https://github.com/example/repo/issues/8' }] }] };
    state.ci = { rows: [] };
    state.linear = { configured: false, issues: [], error: null };
    state.settings.repo_root = '/Users/test/Sites';
    render(state);
  });
  await page.locator('[data-tab="work"]').click();
  await expect(page.locator('[data-panel="prs"]')).toBeVisible();
  await expect(page.locator('#prs')).toContainText('An open pull request');
  await expect(page.locator('#prs')).toContainText('An open issue');
  await expect(page.locator('[data-panel="repos"]')).toContainText('a-private-repository');
  expect(await page.locator('[data-panel]').evaluateAll(panels => panels
    .filter(panel => getComputedStyle(panel).display !== 'none')
    .map(panel => panel.dataset.panel).sort())).toEqual(['ci', 'linear', 'prs', 'repos', 't3activity']);
  await page.locator('#gear').click();
  await expect(page.locator('#set-repo-root')).toBeVisible();
  await expect(page.locator('#set-linear')).toBeVisible();
});

test('portable workspace stays on its work plane and preserves gateway URLs', async ({ page }) => {
  const pageErrors = [];
  page.on('pageerror', error => pageErrors.push(error.message));
  await page.evaluate(() => localStorage.setItem('grave-tab', 'system'));
  await page.goto('./?tab=system');
  await page.evaluate(async () => {
    const state = await (await fetch('api/state')).json();
    state.platform = 'container';
    state.mode = 'developer';
    state.notify = null;
    state.apps = [
      { name: '⌨️ T3 Code', url: '/' },
      { name: '🖥️ Terminal', url: '/term/?arg=shell' },
      { name: '🤖 Claude', url: '/term/?arg=claude' },
    ];
    state.settings.hidden_panels = [];
    render(state);
  });
  await expect(page.locator('body')).toHaveClass(/portable/);
  await expect(page.locator('[data-tab="work"]')).toHaveClass(/active/);
  await expect(page.locator('[data-tab="system"]')).toBeHidden();
  for (const panel of ['stats', 'actions', 'services', 'docker', 'journal']) {
    await expect(page.locator(`[data-panel="${panel}"]`)).toBeHidden();
  }
  await expect(page.locator('[data-panel="repos"]')).toBeVisible();
  await expect(page.locator('#apps')).toContainText('T3 Code');
  await expect(page.locator('#apps a').filter({ hasText: 'Terminal' })).toHaveAttribute('href', '/term/?arg=shell');
  expect(await page.locator('#apps a').filter({ hasText: 'Terminal' }).evaluate(a =>
    new URL(a.href).port === location.port)).toBe(true);
  await page.locator('#gear').click();
  expect(await page.locator('.t3connect-only').evaluateAll(rows =>
    rows.every(row => getComputedStyle(row).display === 'none'))).toBe(true);
  await expect(page.locator('#notify-head')).toBeHidden();
  expect(pageErrors).toEqual([]);
});

test('gamewatch off presents a dev-only UI and can be opted back in', async ({ page }) => {
  await page.locator('[data-tab="system"]').click();
  await page.evaluate(async () => {
    const state = await (await fetch('api/state')).json();
    state.mode = 'developer';
    state.gamewatch = { installed: true, on: false, running: true };
    render(state);
  });
  await expect(page.locator('#mode')).toBeHidden();
  await expect(page.locator('[data-act="gaming"]')).toBeHidden();
  await expect(page.locator('[data-act="developer"]')).toBeHidden();

  await page.locator('#gear').click();
  await expect(page.locator('#throttle-row')).toBeVisible();
  await expect(page.locator('#boot-mode-row')).toBeHidden();

  await page.evaluate(() => applyGamewatch({ installed: true, on: true, running: true }));
  await expect(page.locator('#mode')).toBeVisible();
  await expect(page.locator('#boot-mode-row')).toBeVisible();
  await page.locator('#settings-x').click();
  await expect(page.locator('[data-act="gaming"]')).toBeVisible();
  await expect(page.locator('[data-act="developer"]')).toBeVisible();
});

test('a polling refresh preserves the document scroll position', async ({ page }) => {
  await renderLongMobileRecords(page);
  const before = await page.evaluate(async () => {
    // Let the setup render finish its guarded WebKit anchor restoration before
    // simulating the user's later scroll.
    await new Promise(resolve => requestAnimationFrame(resolve));
    // Keep enough scroll range even when the next poll replaces the deliberately
    // long records with the fixture server's shorter real state.
    document.body.insertAdjacentHTML('beforeend', '<div style="height:1000px" aria-hidden="true"></div>');
    window.scrollTo(0, Math.min(150, document.documentElement.scrollHeight - innerHeight));
    await new Promise(resolve => requestAnimationFrame(resolve));
    return window.scrollY;
  });
  expect(before).toBeGreaterThan(0);

  const after = await page.evaluate(async () => {
    const state = await (await fetch('api/state')).json();
    // Exercise the same render path as the refresh interval with changed live
    // regions both above and below the current iOS viewport.
    state.apps = state.apps.concat({ name: 'Fresh poll result', url: '/fresh' });
    state.system.uptime_s += 5;
    render(state);
    await new Promise(resolve => requestAnimationFrame(resolve));
    return window.scrollY;
  });
  expect(after).toBe(before);
});

test.describe('update dialog API fixtures',()=>{
test.use({serviceWorkers:'block'});
test.afterEach(async({page})=>{await page.unrouteAll({behavior:'wait'});});
async function mockUpdater(page, {mac=false}={}) {
  const fixture={state:'ok',attempt:'previous',dashboard_current:true,log:'previous update',requests:[]};
  await page.route('**/api/admin/releases', route=>route.fulfill({json:{current:'v0.4.0',checkout:'v0.4.0',channel:mac?'edge':'release',releases:['v0.5.0','v0.4.0']}}));
  await page.route('**/api/admin/update-status', route=>route.fulfill({json:{state:fixture.state,attempt:fixture.attempt,dashboard_current:fixture.dashboard_current,log:fixture.log,message:fixture.message,target:fixture.target}}));
  await page.route('**/api/admin/upgrade', route=>{
    const body=route.request().postDataJSON();fixture.requests.push(body);
    fixture.target=body.tag||(body.channel==='configured'?'release':body.channel);
    return route.fulfill({json:{ok:true}});
  });
  const s=await page.evaluate(async()=>await(await fetch('api/state')).json());s.mode='developer';if(mac)s.platform='macos';
  await page.route('**/api/state',route=>route.fulfill({json:s}));await page.evaluate(s=>render(s),s);
  await page.locator('[data-tab="system"]').click();
  await page.locator('#update-open').click();
  await expect(page.getByRole('dialog',{name:/Updates & restart/})).toBeVisible();
  return fixture;
}

test('one update dialog queues exact releases and never mistakes an old success for completion',async({page})=>{
  const x=await mockUpdater(page);
  await expect(page.locator('#grave-release-current')).toContainText('v0.4.0');
  await expect(page.locator('#grave-release')).toHaveValue('configured');
  await expectNoHorizontalOverflow(page,'update dialog');
  await page.locator('#grave-release').selectOption('v0.5.0');
  await page.locator('#install-grave-release').click();
  await expect(page.locator('#grave-release-state')).toContainText('queued');
  expect(x.requests).toEqual([{tag:'v0.5.0'}]);
  await page.evaluate(()=>updateStatus());
  await expect(page.locator('#install-grave-release')).toBeDisabled();
  await expect(page.locator('[data-act="reboot"]')).toBeDisabled();
  x.state='running';x.attempt='new';x.log='Re-running the ritual';
  await page.evaluate(()=>updateStatus());
  await expect(page.locator('#grave-release-state')).toContainText('Updating');
  await page.locator('#update-dialog summary').click();
  await expect(page.locator('#update-log')).toContainText('Re-running the ritual');
  x.state='failed';x.message='installer exited 7';
  await page.evaluate(()=>updateStatus());
  await expect(page.locator('#grave-release-state')).toContainText('installer exited 7');
  await expect(page.locator('#install-grave-release')).toBeEnabled();
});

test('successful updates reload the HTML once and recover the completion dialog',async({page})=>{
  const x=await mockUpdater(page);
  await page.locator('#install-grave-release').click();
  await expect.poll(()=>x.requests).toEqual([{channel:'configured'}]);
  x.attempt='new';x.state='ok';x.dashboard_current=false;
  await page.evaluate(()=>updateStatus());
  await expect(page.locator('#grave-release-state')).toContainText('older files');
  x.dashboard_current=true;
  await Promise.all([page.waitForEvent('framenavigated'),page.evaluate(()=>updateStatus()).catch(()=>{})]);
  await expect(page.locator('#update-dialog')).toBeVisible();
  await expect(page.locator('#grave-release-state')).toContainText('updated dashboard is loaded');
  expect(await page.evaluate(()=>sessionStorage.getItem('grave-update-pending'))).toBeNull();
  expect(x.requests).toHaveLength(1);
});

test('a competing release is never reported as the requested update',async({page})=>{
  const x=await mockUpdater(page);
  await page.locator('#grave-release').selectOption('v0.5.0');
  await page.locator('#install-grave-release').click();
  await expect.poll(()=>x.requests.length).toBe(1);
  x.attempt='other';x.target='v0.4.0';x.state='ok';
  await Promise.all([page.waitForEvent('framenavigated'),page.evaluate(()=>updateStatus()).catch(()=>{})]);
  await expect(page.locator('#grave-release-state')).toContainText('Your selection (v0.5.0) was not confirmed');
});

test('an in-flight update survives reload and reports failure without resubmitting',async({page})=>{
  const x=await mockUpdater(page);
  await page.locator('#install-grave-release').click();
  await expect.poll(()=>x.requests.length).toBe(1);
  x.attempt='new';x.state='running';
  await page.reload();
  await page.evaluate(async()=>render(await(await fetch('api/state')).json()));
  await expect(page.locator('#update-dialog')).toBeVisible();
  await expect(page.locator('#grave-release-state')).toContainText('Updating');
  x.state='failed';x.message='tracked local changes';
  await page.evaluate(()=>updateStatus());
  await expect(page.locator('#grave-release-state')).toContainText('tracked local changes');
  expect(x.requests).toHaveLength(1);
  await page.locator('#update-close').click();
  await expect(page.locator('#update-open')).toBeFocused();
});

test('macOS uses the same updater dialog with its configured channel and no host reboot',async({page})=>{
  const x=await mockUpdater(page,{mac:true});
  await expect(page.locator('[data-act="reboot"]')).toBeHidden();
  await expect(page.locator('[data-act="update-t3"]')).toBeHidden();
  await page.locator('#install-grave-release').click();
  await expect.poll(()=>x.requests).toEqual([{channel:'edge'}]);
  x.attempt='new';x.state='failed';x.message='prior payload restored';
  await page.evaluate(()=>updateStatus());
  await expect(page.locator('#grave-release-state')).toContainText('prior payload restored');
  await page.locator('#grave-release').selectOption('v0.5.0');
  await page.locator('#install-grave-release').click();
  await expect.poll(()=>x.requests[1]).toEqual({tag:'v0.5.0'});
});

});

test('PWA contract spans the appliance origin', async ({ request, baseURL }) => {
  const manifest = await (await request.get(new URL('manifest.webmanifest', baseURL).href)).json();
  expect(manifest.scope).toBe('/');
  expect(manifest.id).toBe('/grave/');
  const worker = await request.get(new URL('sw.js', baseURL).href);
  expect(worker.headers()['service-worker-allowed']).toBe('/');
});

test.describe('issue dispatch API fixtures', () => {
// These tests mock API traffic. A controlling PWA worker can bypass page.route
// after reload; service-worker behavior is covered by the other browser tests.
test.use({ serviceWorkers: 'block' });
// Drain outstanding mocks before Playwright disposes the request context.
test.afterEach(async ({ page }) => {
  await page.unrouteAll({ behavior: 'wait' });
});

test('Linear dispatch chooses a repository and opens the created session', async ({ page }) => {
  let body;
  const s = await page.evaluate(async () => (await fetch('api/state')).json());
    s.dispatch = { available: true, agents: ['codex', 'claude'] };
    s.mode = 'developer';
    s.repos = [{ name: 'a-long-project-name-for-a-phone', branch: 'master', dirty: 0 }];
    s.linear = { configured: true, issues: [{ id: 'GRV-108', title: '<script>literal issue title</script>',
      url: 'https://linear.app/grave/issue/GRV-108/fix', state: 'Todo' }] };
  await page.route('**/api/state', route => route.fulfill({ json: s }));
  await page.route('**/api/linear-dispatch', async route => {
    body = route.request().postDataJSON();
    await route.fulfill({ status: 201, json: { ok: true, output: 'Session started.',
      session: { name: 'linear-grv-108-abcdef', url: '/term/?arg=linear-grv-108-abcdef' } } });
  });
  await page.reload();
  await page.evaluate(async () => render(await (await fetch('api/state')).json()));
  await page.getByRole('button', { name: 'Work on this', exact: true }).click();
  await expect(page.locator('#dispatch-issue')).toHaveText('GRV-108 — <script>literal issue title</script>');
  await expect(page.locator('#dispatch-issue script')).toHaveCount(0);
  await page.locator('#dispatch-start').click();
  await expect(page.locator('#dispatch-message')).toHaveText('Choose a repository and agent.');
  await page.locator('#dispatch-repo').selectOption('a-long-project-name-for-a-phone');
  await page.locator('#dispatch-agent').selectOption('claude');
  await expectNoHorizontalOverflow(page, 'dispatch dialog');
  await page.locator('#dispatch-start').click();
  await expect(page.locator('#dispatch-message a')).toHaveAttribute('href', /\/term\/\?arg=linear-grv-108-abcdef$/);
  expect(body).toEqual({ issue: 'GRV-108', repo: 'a-long-project-name-for-a-phone', agent: 'claude' });
  await page.locator('#dispatch-x').click();
  await expect(page.locator('#dispatch-dlg')).toBeHidden();
});

test('dispatch failures remain actionable and PR links appear beside issue sessions', async ({ page }) => {
  const s = await page.evaluate(async () => (await fetch('api/state')).json());
    s.dispatch = { available: true, agents: ['codex'] };
    s.mode = 'developer';
    s.repos = [{ name: 'project', branch: 'master', dirty: 0 }];
    s.linear = { configured: true, issues: [{ id: 'GRV-108', title: 'Fix the issue',
      url: 'https://linear.app/grave/issue/GRV-108/fix', state: 'Todo' }] };
    s.tmux = [{ name: 'linear-grv-108-abcdef', windows: 1, attached: 'detached',
      worktree: { repo: 'project', branch: 'agent/linear-grv-108-abcdef' },
      dispatch: { agent: 'codex', issue: { id: 'GRV-108', url: 'https://linear.app/grave/issue/GRV-108/fix' }, status: 'exited', exit_code: 0 } }];
  await page.route('**/api/state', route => route.fulfill({ json: s }));
  await page.route('**/api/dispatch-pr?*', route => route.fulfill({ json: {
    pr: { number: 19, url: 'https://github.com/acme/project/pull/19', state: 'OPEN' } } }));
  await page.route('**/api/linear-dispatch', route => route.fulfill({ status: 502,
    json: { ok: false, output: 'Could not fetch this issue from Linear; nothing started.' } }));
  await page.reload();
  await page.evaluate(async () => render(await (await fetch('api/state')).json()));
  await expect(page.locator('#tmux').getByRole('link', { name: /PR #19/ })).toBeVisible();
  await expect(page.locator('#tmux')).toContainText('exited (0)');
  await expectPanelsContainContent(page, 'dispatch session and PR fit');
  await page.getByRole('button', { name: 'Work on this', exact: true }).click();
  await page.locator('#dispatch-repo').selectOption('project');
  await page.locator('#dispatch-start').click();
  await expect(page.locator('#dispatch-message')).toContainText('nothing started');
  await expect(page.locator('#dispatch-start')).toBeEnabled();
});
test('overnight reports escape output and cancel a scheduled job', async ({ page }) => {
  let body;
  const s=await page.evaluate(async()=> (await fetch('api/state')).json());
  s.mode='developer';
  s.scheduled={available:true,jobs:[{name:'nightly',schedule:'02:00',enabled:true,next_due:1800000000}],
    runs:[{name:'nightly',repo:'project',status:'failed',exit_code:9,started:1790000000,
      session:'job-nightly-fixture',log:'session-20260906.log',tail:'<script>literal output</script>'}]};
  await page.route('**/api/state',route=>route.fulfill({json:s}));
  await page.route('**/api/agent-job-cancel',async route=>{
    body=route.request().postDataJSON();
    await route.fulfill({json:{ok:true,output:'cancelled'}});
  });
  await page.reload();
  await page.evaluate(async()=>render(await(await fetch('api/state')).json()));
  await expect(page.locator('[data-panel="scheduled"]')).toBeVisible();
  await page.locator('#scheduled summary').click();
  await expect(page.locator('#scheduled pre')).toHaveText('<script>literal output</script>');
  await expect(page.locator('#scheduled script')).toHaveCount(0);
  await expect(page.getByRole('link',{name:'Open worktree',exact:true})).toHaveAttribute('href',/arg=job-nightly-fixture$/);
  await page.getByRole('button',{name:'Cancel job',exact:true}).click();
  await expect(page.locator('[data-job-cancel]')).toHaveText('Cancelled');
  expect(body).toEqual({name:'nightly'});
  await expectNoHorizontalOverflow(page,'overnight report');
});

});
