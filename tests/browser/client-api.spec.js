const { test, expect } = require('@playwright/test');
test.use({ serviceWorkers: 'block' });
test.afterEach(async ({ page }) => { await page.unrouteAll({ behavior: 'ignoreErrors' }); });
const entry = 'https://home.tail123.ts.net';
const remote = 'https://mac.tail123.ts.net';
const selectedURL = `${entry}/grave/?grave=${encodeURIComponent(remote+'/grave/')}`;

// Both synthetic tailnet origins forward to the real fixture backend. Preserve
// the browser's Origin/preflight headers so the backend enforces actual CORS;
// only the state host label is changed to distinguish the destination.
async function origins(page, request, baseURL, options = {}) {
  const writes = [];
  const trust = await request.post(new URL('api/client-trust', baseURL).href, { data: { origins: [entry] } });
  expect(trust.ok()).toBeTruthy();
  const fixture = await (await request.get(new URL('api/state', baseURL).href)).json();
  await page.route('https://*.tail123.ts.net/**', async route => {
    const req = route.request(), url = new URL(req.url()), isRemote = url.origin === remote;
    if (isRemote && options.offline) return route.abort('connectionfailed');
    if (isRemote && req.method() === 'POST') writes.push(url.pathname);
    const headers = { ...req.headers(), 'Tailscale-User-Login': options.denied && isRemote ? 'outsider@example.test' : 'browser@example.test' };
    delete headers.host;
    const response = await request.fetch(new URL(url.pathname + url.search, baseURL).href, {
      method: req.method(), headers, data: req.postDataBuffer() || undefined,
    });
    if (isRemote && url.pathname.endsWith('/api/v1/state') && response.ok()) {
      return route.fulfill({ response, json: { ...fixture, host: 'REMOTE-MAC', mode: 'developer' } });
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

test('Manage here preserves entry origin and sends settings only to selected grave', async ({ page, request, baseURL }) => {
  const writes = await origins(page, request, baseURL);
  await page.goto(entry+'/grave/');
  await page.locator('#graveyard-toggle').click();
  await page.locator('[data-plot="remote"] [data-plot-details]').click();
  await page.locator('[data-plot="remote"] [data-plot-action="manage"]').click();
  await expect(page.locator('#plot-context')).toContainText('REMOTE-MAC');
  expect(new URL(page.url()).origin).toBe(entry);
  await page.locator('[data-tab="system"]').click();
  await page.locator('[data-settings="dashboard"]').click();
  await expect(page.locator('#client-trust-settings')).toBeHidden();
  await page.locator('#save-set').click();
  await expect(page.locator('#set-msg')).toHaveText('saved ✓');
  expect(writes).toEqual(['/grave/api/v1/settings']);
  await page.reload();
  await expect(page.locator('#plot-context')).toContainText('REMOTE-MAC');
  await expect(page.locator('#plot-context')).toHaveAttribute('title', /mac\.tail123\.ts\.net/);
  const hrefs = await page.locator('#apps a[href]').evaluateAll(nodes => nodes.map(n => n.href));
  expect(hrefs.some(h => h.startsWith(remote))).toBeTruthy();
  expect(hrefs.some(h => h.startsWith(entry+'/term'))).toBeFalsy();
  await page.locator('#remote-home').click();
  await expect(page).toHaveURL(entry+'/grave/?tab=system');
  await expect(page.locator('#plot-context')).not.toContainText('REMOTE-MAC');
});

test('remote errors keep destination selected and never display entry state', async ({ page, request, baseURL }) => {
  const writes = await origins(page, request, baseURL, { offline: true });
  await page.goto(selectedURL);
  await expect(page.locator('#connection')).toHaveClass(/show/);
  await expect(page.locator('#connection-text')).toContainText('mac.tail123.ts.net');
  await expect(page.locator('body')).toHaveClass(/remote-pending/);
  expect(new URL(page.url()).searchParams.get('grave')).toBe(remote+'/grave/');
  expect(writes).toEqual([]);
});

test('destination authorization remains required for a trusted dashboard', async ({ page, request, baseURL }) => {
  await origins(page, request, baseURL, { denied: true });
  await page.goto(selectedURL);
  await expect(page.locator('#connection-text')).toContainText('403');
  await expect(page.locator('#panels')).toBeHidden();
});

test('invalid selected endpoint never falls back to local reads or writes', async ({ page }) => {
  await page.goto('./?grave=https%3A%2F%2Fevil.example%2Fgrave%2F');
  await expect(page.locator('#connection-text')).toContainText('Invalid grave address');
  await expect(page.locator('#panels')).toBeHidden();
  await expect(page.locator('#plot-context')).toHaveText('Invalid grave');
  await page.goto('./?grave=');
  await expect(page.locator('#connection-text')).toContainText('Invalid grave address');
  await expect(page.locator('#panels')).toBeHidden();
});

async function operationFixture(page, request, baseURL, options={}) {
  await origins(page, request, baseURL);
  const starts=[];let record=null,reads=0;
  await page.route(remote+'/grave/api/v1/operations*', async route=>{
    const req=route.request();
    const headers={'Access-Control-Allow-Origin':entry,'Vary':'Origin','Content-Type':'application/json',
      'Access-Control-Allow-Headers':'Content-Type, X-Grave-Client','Access-Control-Allow-Methods':'POST, GET'};
    if(req.method()==='OPTIONS')return route.fulfill({status:204,headers});
    if(req.method()==='POST'){
      const body=req.postDataJSON();starts.push(body);
      if(options.lostBeforeSubmit)return route.abort('connectionfailed');
      record={...body,state:'running',events:[{seq:1,text:'started once\n'}],cursor:1};
      if(options.lostResponse)return route.abort('connectionfailed');
      return route.fulfill({status:202,headers,json:record});
    }
    reads++;
    if(!record||options.expired)return route.fulfill({status:404,headers,json:{output:'Operation not found or history expired; it has not been restarted'}});
    const after=Number(new URL(req.url()).searchParams.get('after'));
    const state=options.finish?'succeeded':options.interrupted?'interrupted':'running';
    return route.fulfill({headers,json:{...record,state,message:options.interrupted?'Dashboard restarted; outcome unknown. Check the grave.':'',events:record.events.filter(e=>e.seq>after)}});
  });
  return {starts,get reads(){return reads;}};
}

test('lost operation response resumes the same ID after reload without another start', async ({page,request,baseURL})=>{
  const options={lostResponse:true};
  const fixture=await operationFixture(page,request,baseURL,options);
  await page.goto(selectedURL);
  await page.evaluate(()=>{runStream('doctor');});
  await expect(page.locator('#console-out')).toContainText('Use Resume action');
  expect(fixture.starts).toHaveLength(1);
  const id=fixture.starts[0].id;
  await page.reload();
  await expect(page.locator('#operation-resume')).toContainText('Resume action · doctor · mac.tail123.ts.net');
  options.finish=true;
  await page.locator('#operation-resume').click();
  await expect(page.locator('#console-out')).toContainText('sequence complete');
  await expect(page.locator('#console-out')).toContainText('started once');
  expect(fixture.starts).toEqual([{id,action:'doctor'}]);
  expect(fixture.reads).toBeGreaterThan(0);
  await page.locator('#console-x').click();
  await page.locator('#remote-home').click();
  await expect(page.locator('#operation-resume')).toBeHidden();
});

test('closing progress preserves tracking and restart uncertainty does not replay an action',async ({page,request,baseURL})=>{
  const options={};
  const fixture=await operationFixture(page,request,baseURL,options);
  await page.goto(selectedURL);
  await page.evaluate(()=>{runStream('doctor');});
  await expect(page.locator('#console-out')).toContainText('started once');
  await page.locator('#console-x').click();
  await expect(page.locator('#console')).toBeHidden();
  await expect(page.locator('#operation-resume')).toBeVisible();
  options.interrupted=true;
  await page.reload();
  await page.locator('#operation-resume').click();
  await expect(page.locator('#console-out')).toContainText('outcome unknown');
  expect(fixture.starts).toHaveLength(1);
  await page.locator('#console-close').click();
  options.expired=true;
  await page.locator('#operation-resume').click();
  await expect(page.locator('#console-out')).toContainText('history expired');
  expect(fixture.starts).toHaveLength(1);
});


test('an unreceived submission retries only its saved ID',async ({page,request,baseURL})=>{
  const options={lostBeforeSubmit:true};
  const fixture=await operationFixture(page,request,baseURL,options);
  await page.goto(selectedURL);
  await page.evaluate(()=>{runStream('doctor');});
  await expect(page.locator('#console-out')).toContainText('Use Resume action');
  expect(fixture.starts).toHaveLength(1);
  options.lostBeforeSubmit=false;options.finish=true;
  await page.locator('#console-x').click();
  await page.locator('#operation-resume').click();
  await expect(page.locator('#console-out')).toContainText('sequence complete');
  expect(fixture.starts).toHaveLength(2);
  expect(fixture.starts[1]).toEqual(fixture.starts[0]);
});
