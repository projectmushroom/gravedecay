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
      { name: 'claude-yolo-with-a-deliberately-long-session-name', windows: 12, attached: 'detached' },
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
  await expect(page.locator('[data-act="update-grave"]')).toBeVisible();
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

test('an administrator can select an exact stable release', async ({ page }) => {
  // WebKit's registered service worker owns requests before Playwright's
  // route mock can see them. Stub fetch in-page for this UI-only interaction;
  // Python contract tests exercise the real API and exact systemd command.
  await page.evaluate(() => {
    const realFetch = window.fetch.bind(window);
    window.requestedReleaseTag = null;
    window.fetch = (input, init = {}) => {
      const url = typeof input === 'string' ? input : input.url;
      if (url.endsWith('api/admin/releases')) return Promise.resolve(new Response(JSON.stringify({
        current: 'v0.4.0', checkout: 'v0.4.0', releases: ['v0.5.0', 'v0.4.0'],
      }), { status: 200, headers: { 'Content-Type': 'application/json' } }));
      if (url.endsWith('api/admin/upgrade')) {
        window.requestedReleaseTag = JSON.parse(init.body).tag;
        return Promise.resolve(new Response(JSON.stringify({ ok: true }), {
          status: 200, headers: { 'Content-Type': 'application/json' },
        }));
      }
      return realFetch(input, init);
    };
  });
  await page.locator('[data-tab="system"]').click();
  await expect(page.locator('#grave-release')).toHaveValue('v0.4.0');
  await page.locator('#grave-release').selectOption('v0.5.0');
  page.once('dialog', dialog => dialog.accept());
  await page.locator('#install-grave-release').click();
  await expect(page.locator('#grave-release-state')).toContainText('v0.5.0 queued');
  expect(await page.evaluate(() => window.requestedReleaseTag)).toBe('v0.5.0');
});

test('PWA contract spans the appliance origin', async ({ request, baseURL }) => {
  const manifest = await (await request.get(new URL('manifest.webmanifest', baseURL).href)).json();
  expect(manifest.scope).toBe('/');
  expect(manifest.id).toBe('/grave/');
  const worker = await request.get(new URL('sw.js', baseURL).href);
  expect(worker.headers()['service-worker-allowed']).toBe('/');
});
