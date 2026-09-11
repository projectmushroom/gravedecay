// Exercise the shipped backend in an iPhone-sized browser with an empty HOME.
const { webkit, devices } = require('@playwright/test');
const { spawn } = require('node:child_process');
const { mkdtempSync, rmSync } = require('node:fs');
const { tmpdir } = require('node:os');
const path = require('node:path');
const assert = require('node:assert/strict');

(async () => {
  assert(process.env.GRAVEDECAY_TEST_APP, 'GRAVEDECAY_TEST_APP must name the built/mounted app');
  const root = mkdtempSync(path.join(tmpdir(), 'grave-pwa-'));
  const bundle = path.join(process.env.GRAVEDECAY_TEST_APP, 'Contents/Resources/NativeHost');
  const architecture = process.arch === 'arm64' ? 'aarch64' : 'x86_64';
  const child = spawn(path.join(bundle, architecture, 'python/bin/python3'),
    ['-I', '-B', '-u', path.join(bundle, 'host.py'), '--root', path.join(root, 'data'), '--port', '0', '--network-port', '0'],
    { env: { HOME: root, PATH: '/usr/bin:/bin:/usr/sbin:/sbin', GRAVEDECAY_ALLOWED_USERS: 'browser@example.test' }, stdio: ['pipe', 'pipe', 'pipe'] });
  let browser, errors = '';
  child.stderr.on('data', data => { errors = (errors + data).slice(-8192); });
  try {
    const ready = await new Promise((resolve, reject) => {
      const timer = setTimeout(() => reject(Error('Host startup timed out: ' + errors)), 45000);
      let output = '';
      child.on('error', error => { clearTimeout(timer); reject(error); });
      child.on('exit', code => { clearTimeout(timer); reject(Error(`Host exited ${code}: ${errors}`)); });
      child.stdout.on('data', data => {
        output += data;
        if (output.includes('\n')) { clearTimeout(timer); resolve(JSON.parse(output)); }
      });
    });
    browser = await webkit.launch();
    const context = await browser.newContext({ ...devices['iPhone 13'], extraHTTPHeaders: { 'Tailscale-User-Login': 'browser@example.test' } });
    // Model Serve's one HTTPS origin and prefix stripping without changing
    // the developer's tailnet or reserving production ports.
    await context.route('https://native-host.test/**', async route => {
      const url = new URL(route.request().url());
      const network = url.pathname.startsWith('/net/');
      const pathname = network ? url.pathname.slice(4) : url.pathname;
      const response = await route.fetch({ url: `http://127.0.0.1:${network ? ready.network_port : ready.port}${pathname}${url.search}` });
      await route.fulfill({ response });
    });
    const page = await context.newPage();
    const pageErrors = [];
    page.on('pageerror', error => pageErrors.push(error.message));
    const response = await page.goto('https://native-host.test/grave/');
    assert.equal(response.status(), 200);
    await page.locator('body.macnative').waitFor({ timeout: 30000 });
    assert(await page.locator('#plot-context').innerText(), 'Missing host identity');
    assert.equal(await page.locator('#update-notice').isVisible(), false);
    assert.deepEqual(pageErrors, []);
    await page.locator('#apps a').filter({ hasText: 'Network' }).waitFor();
    const networkURL = await page.locator('#apps a').filter({ hasText: 'Network' }).getAttribute('href');
    assert.equal(new URL(networkURL, page.url()).pathname, '/net/');
    const manifest = await page.evaluate(async () => {
      const url = document.querySelector('link[rel="manifest"]').href;
      const data = await (await fetch(url)).json();
      return { start: new URL(data.start_url, url).pathname, scope: data.scope, display: data.display };
    });
    assert.deepEqual(manifest, { start: '/grave/', scope: '/', display: 'standalone' });
    assert.deepEqual(pageErrors, []);
    console.log('Packaged native host: iPhone WebKit dashboard, state, network link and PWA manifest passed.');
  } finally {
    if (browser) await browser.close();
    child.stdin.end();
    if (child.exitCode === null) await new Promise(resolve => {
      const timer = setTimeout(() => { child.kill('SIGKILL'); resolve(); }, 5000);
      child.once('exit', () => { clearTimeout(timer); resolve(); });
    });
    rmSync(root, { recursive: true, force: true });
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
