const { test, expect } = require('@playwright/test');
// Route fixtures must also intercept requests after the PWA claims the page.
test.use({ serviceWorkers: 'block' });

const plot = (id, platform = 'linux', links = { dashboard: '/grave/', t3: '/', terminal: '/term/', t3_setup: '/grave/' }) => ({
  id, dns: `${id}.tail.ts.net`, name: id === 'mac' ? 'My Mac' : 'My VM', lastSeen: Date.now() / 1000,
  summary: { product: 'gravedecay', api_version: 1, node: { host: id, platform, mode: 'developer' },
    resources: { cpu_pct: 12, memory_pct: 34, disk_pct: 56 }, activity: { sessions_live: 2 }, health: { services_failed: 0, containers_problem: 0 }, links },
});

const openGraveyard = page => page.locator('#graveyard-toggle').click();
const details = (page, id) => page.locator(`[data-plot="${id}"] [data-plot-details]`).click();

test('compact switcher keeps the launcher visible and contains a long inventory', async ({ page }) => {
  const plots = Array.from({ length: 20 }, (_, i) => ({ ...plot(`vm-${i}`), name: `Work box ${i}` }));
  await page.route('**/api/graveyard', route => route.fulfill({ json: { state: 'ready', plots } }));
  await page.goto('./');
  await expect(page.locator('#graveyard-count')).toHaveText(' · 20');
  await expect(page.locator('#graveyard-dialog')).not.toBeVisible();
  expect((await page.locator('#graveyard-toggle').boundingBox()).height).toBeLessThanOrEqual(72);
  await expect(page.locator('#apps')).toBeInViewport();
  await openGraveyard(page);
  await expect(page.locator('#graveyard-close')).toBeFocused();
  await expect(page.locator('.plot-detail:visible')).toHaveCount(0);
  expect((await page.locator('.plot-card').first().boundingBox()).height).toBeLessThanOrEqual(140);
  const bounds = await page.locator('#graveyard-dialog').boundingBox();
  expect(bounds.y).toBeGreaterThanOrEqual(0);
  expect(bounds.y + bounds.height).toBeLessThanOrEqual(page.viewportSize().height);
  expect(await page.locator('.graveyard-body').evaluate(el => el.scrollHeight > el.clientHeight)).toBe(true);
  await page.getByLabel('Find a grave').fill('vm-19.tail');
  await expect(page.locator('.plot-card:visible')).toHaveCount(1);
  await details(page, 'vm-19');
  await expect(page.locator('[data-plot="vm-19"] .plot-detail')).toBeVisible();
  await page.getByRole('button', { name: 'Refresh plots' }).click();
  await expect(page.locator('[data-plot="vm-19"] .plot-detail')).toBeVisible();
  await expect(page.getByLabel('Find a grave')).toHaveValue('vm-19.tail');
  await page.getByLabel('Find a grave').fill('no-such-grave');
  await expect(page.locator('#graveyard-empty')).toBeVisible();
  await page.getByRole('button', { name: 'Close Graveyard' }).click();
  await expect(page.locator('#graveyard-toggle')).toBeFocused();
  // A background discovery refresh must not reopen the switcher.
  await page.evaluate(() => refreshGraveyard());
  await expect(page.locator('#graveyard-dialog')).not.toBeVisible();
  await openGraveyard(page);
  await page.keyboard.press('Escape');
  await expect(page.locator('#graveyard-dialog')).not.toBeVisible();
  await expect(page.locator('#graveyard-toggle')).toBeFocused();
  await expect(page.locator('body')).not.toHaveCSS('overflow', 'hidden');
});

test('local apps and remote graves use explicit top-level links without popups', async ({ page, context, request, baseURL }) => {
  const home = plot('home'), remote = plot('mac', 'macos');
  // Serve the real shell under a synthetic HTTPS origin. No tailnet traffic.
  await page.route('https://home.tail.ts.net/**', async route => {
    const url = new URL(route.request().url());
    if (url.pathname.includes('/api/') && !url.pathname.endsWith('/api/state')) {
      await route.fulfill({ json: {} });
      return;
    }
    const response = await request.get(new URL(url.pathname + url.search, baseURL).href);
    await route.fulfill({ response });
  });
  await page.route('**/api/graveyard', route => route.fulfill({ json: { state: 'ready', plots: [remote, home] } }));
  await page.addInitScript(() => Object.defineProperty(navigator, 'standalone', { value: true }));
  await page.goto('https://home.tail.ts.net/grave/');
  await openGraveyard(page);
  await expect(page.locator('.plot-card').first()).toHaveAttribute('data-plot', 'home');
  await expect(page.locator('[data-plot="home"]')).toContainText('This grave');
  await expect(page.locator('#graveyard-pwa-note')).toBeVisible();
  await details(page, 'home');
  await page.locator('[data-plot="home"] [data-plot-action="t3_setup"]').click();
  await expect(page.locator('#graveyard-dialog')).not.toBeVisible();
  await expect(page.locator('#settings-panel')).toBeVisible();
  await expect(page.getByRole('button', { name: 'New T3 pairing token' })).toBeFocused();
  await page.locator('#settings-x').click();
  await openGraveyard(page);
  const localT3 = page.locator('[data-plot="home"] [data-plot-action="t3"]');
  await expect(localT3).toHaveAttribute('target', '_self');
  await expect(localT3.locator('[aria-label="another grave"]')).toHaveCount(0);
  await page.route('https://home.tail.ts.net/', route => route.fulfill({ contentType: 'text/html', body: '<h1>Local T3</h1>' }));
  await localT3.click();
  await expect(page).toHaveURL('https://home.tail.ts.net/');
  expect(context.pages()).toHaveLength(1);
  await page.goBack();
  if (!await page.locator('#graveyard-dialog').isVisible()) await openGraveyard(page);
  const openRemote = page.locator('[data-plot="mac"] [data-plot-action="dashboard"]');
  await expect(openRemote).toHaveAttribute('target', '_self');
  await expect(openRemote.locator('[aria-label="another grave"]')).toBeVisible();
  await page.route('https://mac.tail.ts.net/grave/', route => route.fulfill({ contentType: 'text/html', body: '<h1>Remote grave</h1>' }));
  await openRemote.click();
  await expect(page).toHaveURL('https://mac.tail.ts.net/grave/');
  await expect(page.getByRole('heading', { name: 'Remote grave' })).toBeVisible();
  expect(context.pages()).toHaveLength(1);
  // Safari's installed-PWA browser sheet itself needs a real iOS device;
  // this checks our navigation intent, not OS-owned presentation.
  await page.unrouteAll({ behavior: 'ignoreErrors' });
});

test('saved plots survive a reload and offline graves never navigate elsewhere', async ({ page }) => {
  let plots = [plot('mac', 'macos'), plot('vm')];
  await page.route('**/api/graveyard', route => route.fulfill({ json: { state: 'ready', plots } }));
  await page.goto('./');
  await openGraveyard(page);
  await expect(page.locator('.plot-card')).toHaveCount(2);
  await expect(page.locator('[data-plot="vm"] a').first()).toHaveAttribute('href', 'https://vm.tail.ts.net/grave/');
  await details(page, 'vm');
  await expect(page.locator('[data-plot="vm"]').getByRole('link', { name: 'Set up T3' })).toHaveAttribute('href', 'https://vm.tail.ts.net/grave/#t3-setup');
  await expect(page.locator('[data-plot="vm"]')).toContainText('CPU 12% · Memory 34% · Disk 56%');
  await page.getByLabel('Find a grave').fill('mac');
  plots = [plot('vm')];
  await page.reload();
  await expect(page.locator('#graveyard-dialog')).not.toBeVisible();
  await openGraveyard(page);
  await expect(page.getByLabel('Find a grave')).toHaveValue('');
  await expect(page.locator('.plot-card')).toHaveCount(2);
  await expect(page.locator('[data-plot="mac"]')).toContainText('Unreachable');
  await expect(page.locator('[data-plot="mac"] a')).toHaveCount(0);
  await details(page, 'mac');
  await page.getByRole('button', { name: 'Forget plot' }).click();
  await expect(page.locator('.plot-card')).toContainText('My VM');
  await expect(page).toHaveURL(/\/$/);
});

test('overview fits phones and only offers published safe links', async ({ page }) => {
  const mac = plot('mac', 'macos', { terminal: '//external.example', dashboard: '/grave/../term', t3_setup: '/grave/?token=secret' });
  mac.name = 'My very long plot name with no spaces: ' + 'x'.repeat(130);
  await page.route('**/api/graveyard', route => route.fulfill({ json: { state: 'ready', plots: [mac, plot('vm')] } }));
  await page.goto('./');
  await openGraveyard(page);
  await details(page, 'mac');
  await expect(page.locator('.plot-card')).toHaveCount(2);
  await expect(page.locator('[data-plot="mac"] a')).toHaveCount(0);
  const dimensions = await page.evaluate(() => ({ width: document.documentElement.clientWidth, content: document.documentElement.scrollWidth }));
  expect(dimensions.content).toBeLessThanOrEqual(dimensions.width);
});

test('T3 setup opens pairing controls without minting a token', async ({ page }) => {
  let actions = 0;
  await page.route('**/api/action-stream?**', route => {
    actions++;
    return route.fulfill({ contentType: 'text/event-stream', body: 'event: done\ndata: {"code":0}\n\n' });
  });
  await page.goto('./#t3-setup');
  await expect(page.locator('#settings-panel')).toBeVisible();
  const pair = page.getByRole('button', { name: 'New T3 pairing token' });
  await expect(pair).toBeInViewport();
  await expect(pair).toBeFocused();
  await expect(page.locator('#t3-setup')).toContainText('Set up T3 ·');
  await expect(page.locator('#settings-title')).toContainText('Settings ·');
  expect(actions).toBe(0);
  expect(new URL(page.url()).hash).toBe('');
  await pair.click();
  await expect.poll(() => actions).toBe(1);
  await expect(page.locator('#console-title')).toContainText(new URL(page.url()).hostname);
});

test('plot identity persists while connection and T3 states change independently', async ({ page }) => {
  const mac = plot('mac', 'macos'), vm = plot('vm');
  mac.summary.health.t3 = 'running'; vm.summary.health.t3 = 'stopped';
  let state = 'ready', plots = [mac, vm];
  await page.route('**/api/graveyard', route => route.fulfill({ json: { state, plots } }));
  await page.goto('./');
  await openGraveyard(page);
  await details(page, 'mac');
  await details(page, 'vm');
  await expect(page.locator('[data-plot="mac"]')).toContainText('Dashboard reachable · T3 service running');
  await expect(page.locator('[data-plot="mac"] h3')).toContainText('My Mac');
  await expect(page.locator('[data-plot="vm"]')).toContainText('Dashboard reachable · T3 stopped');
  await expect(page.locator('[data-plot="mac"]')).toHaveCSS('border-left-color', 'rgb(195, 153, 237)');
  await page.reload();
  await openGraveyard(page);
  await page.getByLabel('Find a grave').fill('vm');
  await details(page, 'vm');
  await expect(page.locator('[data-plot="vm"]')).toHaveCSS('border-left-color', 'rgb(241, 148, 176)');
  state = 'scanning';
  await page.getByRole('button', { name: 'Refresh plots' }).click();
  await expect(page.locator('[data-plot="vm"]')).toContainText('Checking connection');
  await expect(page.locator('.plot-card a')).toHaveCount(0);
  state = 'ready'; vm.summary.health.t3 = 'failed';
  await page.getByRole('button', { name: 'Refresh plots' }).click();
  await expect(page.locator('[data-plot="vm"]')).toContainText('Dashboard reachable · T3 service failed');
  await expect(page.locator('[data-plot="vm"] .plot-detail')).toBeVisible();
  plots = [mac];
  await page.getByRole('button', { name: 'Refresh plots' }).click();
  await expect(page.locator('[data-plot="vm"]')).toContainText('Unreachable — check Tailscale or dashboard');
  await expect(page.locator('[data-plot="vm"]')).not.toContainText('T3 service failed');
  await expect(page.getByLabel('Find a grave')).toHaveValue('vm');
});

test('revoked overview access hides remembered inventory', async ({ page }) => {
  let denied = false;
  await page.route('**/api/graveyard', route => denied
    ? route.fulfill({ status: 403, json: { error: 'forbidden' } })
    : route.fulfill({ json: { state: 'ready', plots: [plot('mac')] } }));
  await page.goto('./');
  await openGraveyard(page);
  await expect(page.locator('.plot-card')).toHaveCount(1);
  denied = true;
  await page.getByRole('button', { name: 'Refresh plots' }).click();
  await expect(page.locator('#graveyard-status')).toContainText('Sign in');
  await expect(page.locator('.plot-card')).toHaveCount(0);
});

test('OS logos identify current and saved graves without remote assets', async ({ page }) => {
  const arch = plot('arch'), mac = plot('mac', 'macos'), unknown = plot('old');
  arch.summary.node.os_icon = 'archlinux'; arch.summary.node.os_name = 'Omarchy';
  const malicious = plot('bad'); malicious.summary.node.os_icon = 'https://evil.test/logo.svg';
  malicious.summary.node.os_name = '<img src=x onerror=alert(1)>';
  await page.route('**/api/graveyard', route => route.fulfill({ json: { state: 'ready', plots: [arch, mac, unknown, malicious] } }));
  await page.route('**/api/state', route => route.abort());
  await page.goto('./');
  await page.evaluate(() => render({ ...BOOT, os_icon: 'archlinux', os_name: 'Omarchy', host: 'My Arch grave' }));
  await expect(page.locator('#plot-context use')).toHaveAttribute('href', '#os-archlinux');
  await expect(page.locator('#plot-context')).toContainText('My Arch grave');
  await expect(page.locator('#settings-title use')).toHaveAttribute('href', '#os-archlinux');
  await expect(page.locator('#update-title use')).toHaveAttribute('href', '#os-archlinux');
  await openGraveyard(page);
  for (const [id, icon] of [['arch','archlinux'],['mac','apple'],['old','tux'],['bad','tux']]) {
    await expect(page.locator(`[data-plot="${id}"] use`)).toHaveAttribute('href', `#os-${icon}`);
    expect(await page.locator(`[data-plot="${id}"] use`).evaluate(el => el.getBBox().width)).toBeGreaterThan(0);
  }
  await expect(page.locator('[data-plot="arch"] svg')).toHaveAccessibleName('Omarchy');
  await expect(page.locator('[data-plot="bad"] img')).toHaveCount(0);
  // The last known OS remains recognizable after discovery loses contact.
  await page.route('**/api/graveyard', route => route.fulfill({ json: { state: 'ready', plots: [] } }));
  await page.reload();
  await openGraveyard(page);
  await expect(page.locator('[data-plot="arch"] use')).toHaveAttribute('href', '#os-archlinux');
  await expect(page.locator('[data-plot="arch"]')).toContainText('Unreachable');
});
