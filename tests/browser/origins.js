// Shared by the client API specs. Both synthetic tailnet origins forward to the
// real fixture backend. Preserve the browser's Origin/preflight headers so the
// backend enforces actual CORS; only the state host label is changed to
// distinguish the destination (options.state merges more into it).
const { expect } = require('@playwright/test');
const entry = 'https://home.tail123.ts.net';
const remote = 'https://mac.tail123.ts.net';
const selectedURL = `${entry}/grave/?grave=${encodeURIComponent(remote+'/grave/')}`;

async function origins(page, request, baseURL, options = {}) {
  const writes = [];
  const trust = await request.post(new URL('api/client-trust', baseURL).href, { data: { origins: [entry] } });
  expect(trust.ok()).toBeTruthy();
  const fixture = await (await request.get(new URL('api/state', baseURL).href)).json();
  await page.route('https://*.tail123.ts.net/**', async route => {
    const req = route.request(), url = new URL(req.url()), isRemote = url.origin === remote;
    if (isRemote && options.offline) return route.abort('connectionfailed');
    if (isRemote && req.method() === 'POST') writes.push(url.pathname);
    const headers = { ...req.headers(), 'X-Forwarded-Proto': 'https', 'Tailscale-User-Login': options.denied && isRemote ? 'outsider@example.test' : 'browser@example.test' };
    delete headers.host;
    const response = await request.fetch(new URL(url.pathname + url.search, baseURL).href, {
      method: req.method(), headers, data: req.postDataBuffer() || undefined,
    });
    if (isRemote && url.pathname.endsWith('/api/v1/state') && response.ok()) {
      return route.fulfill({ response, json: { ...fixture, host: 'REMOTE-MAC', mode: 'developer', ...(options.state || {}) } });
    }
    if (url.pathname.endsWith('/api/graveyard')) {
      return route.fulfill({ response, json: { state: 'ready', plots: [{
        id: 'remote', dns: 'mac.tail123.ts.net', name: 'Remote Mac', lastSeen: Date.now()/1000,
        summary: { product: 'gravedecay', api_version: 1, node: { host: 'REMOTE-MAC', platform: 'linux', mode: 'developer' },
          resources: {}, activity: {}, health: {}, links: { dashboard: '/grave/', t3: '/' } },
      }] } });
    }
    return route.fulfill({ response });
  });
  return writes;
}

module.exports = { origins, entry, remote, selectedURL };
