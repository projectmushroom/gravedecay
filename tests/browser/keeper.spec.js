const { test } = require('@playwright/test');
// Replies stream one event per one-second read; give completions time to land.
const expect = require('@playwright/test').expect.configure({ timeout: 10000 });
const { origins, entry, remote, selectedURL } = require('./origins');
test.use({ serviceWorkers: 'block' });
test.afterEach(async ({ page }) => { await page.unrouteAll({ behavior: 'ignoreErrors' }); });

const cors = { 'Access-Control-Allow-Origin': entry, 'Vary': 'Origin', 'Content-Type': 'application/json',
  'Access-Control-Allow-Headers': 'Content-Type, X-Grave-Client', 'Access-Control-Allow-Methods': 'POST, GET' };
const notFound = { output: 'Conversation not found for this owner, or expired' };
const script = [
  { type: 'text', text: 'Looking at the services…' },
  { type: 'tool_call', name: 'mcp__keeper__get_logs', text: '{"target":"dash"}' },
  { type: 'tool_result', name: 'mcp__keeper__get_logs', text: 'Sep 22 dashboard started' },
  { type: 'text', text: 'Nothing is failing. <b>not html</b>' },
  { type: 'done', text: '' },
];

// A scripted Keeper on the remote grave, answering the documented record shape.
// The fixture backend on the test machine has no provider, so the events come
// from here: one script event per cursor read, the last one settling the state.

async function keeperFixture(page, request, baseURL, options = {}) {
  await origins(page, request, baseURL, options);
  const posts = []; let record = null;
  const view = after => { const { pending, ...rest } = record; return { ...rest, events: record.events.filter(e => e.seq > after) }; };
  await page.route(remote + '/grave/api/v1/keeper**', async route => {
    const req = route.request(), url = new URL(req.url()), path = url.pathname.replace('/grave/api/v1/', '');
    if (req.method() === 'OPTIONS') return route.fulfill({ status: 204, headers: cors });
    if (req.method() === 'POST') {
      const body = req.postDataJSON(); posts.push({ path, body });
      if (path === 'keeper') {
        if (record && record.id === body.id) return route.fulfill({ status: 200, headers: cors, json: view(0) });
        record = { id: body.id, provider: 'claude', model: '', session_id: null, host: 'REMOTE-MAC', owner: 'browser@example.test',
          created_at: 1, updated_at: 1, summary: '', state: 'idle', turn: null, cursor: 0, events: [], truncated: false, message: '', pending: [] };
        return route.fulfill({ status: 202, headers: cors, json: view(0) });
      }
      if (!record || record.id !== body.conversation) return route.fulfill({ status: 404, headers: cors, json: notFound });
      if (path === 'keeper/cancel') {
        if (record.state === 'running') { record.state = 'cancelled'; record.message = 'Cancelled by the owner'; record.pending = []; }
        return route.fulfill({ status: 200, headers: cors, json: view(0) });
      }
      if (record.turn && record.turn.id === body.turn) return route.fulfill({ status: 200, headers: cors, json: view(0) });
      if (record.state === 'running') return route.fulfill({ status: 409, headers: cors, json: { error: 'busy', output: 'A turn is already running in this conversation' } });
      if (options.lostBeforeTurn) { options.lostBeforeTurn = false; return route.abort('connectionfailed'); }
      record.state = 'running'; record.turn = { id: body.turn, started_at: 2, finished_at: null, exit_code: null };
      record.events.push({ seq: ++record.cursor, type: 'text', name: 'owner', text: body.message, at: 2 });
      record.pending = (options.script || script).slice();
      if (options.lostTurnResponse) { options.lostTurnResponse = false; return route.abort('connectionfailed'); }
      return route.fulfill({ status: 202, headers: cors, json: view(0) });
    }
    const id = url.searchParams.get('id');
    if (!id) return route.fulfill({ headers: cors, json: { conversations: record ? [{ ...view(0), events: undefined }] : [], provider: 'claude', model: '' } });
    if (!record || record.id !== id) return route.fulfill({ status: 404, headers: cors, json: notFound });
    if (record.state === 'running' && record.pending.length) {
      const event = record.pending.shift();
      record.events.push({ seq: ++record.cursor, name: '', ...event, at: 3 });
      if (!record.pending.length && !options.hold) {
        record.state = options.finalState || 'succeeded'; record.message = options.finalMessage || ''; record.turn.finished_at = 4;
      }
    }
    return route.fulfill({ headers: cors, json: view(Number(url.searchParams.get('after') || 0)) });
  });
  return { posts, get record() { return record; } };
}

async function summon(page) {
  await page.goto(selectedURL);
  await page.locator('#keeper-open').click();
  await expect(page.locator('#keeper')).toBeVisible();
  await expect(page.locator('#keeper-host')).toHaveText('mac.tail123.ts.net');
}

test('a prompt chip streams a text-only reply with tool activity and the conversation survives reload', async ({ page, request, baseURL }) => {
  const fixture = await keeperFixture(page, request, baseURL);
  await summon(page);
  await expect(page.locator('#keeper-status')).toContainText('claude');
  await expect(page.locator('#keeper-input')).toBeFocused();
  await page.locator('[data-keeper-chip="Run doctor"]').click();
  await expect(page.locator('#keeper-log .keeper-msg.owner')).toHaveText('Run doctor');
  await expect(page.locator('#keeper-status')).toHaveText('Reply complete.');
  const tool = page.locator('#keeper-log details.keeper-tool');
  await expect(tool).toHaveCount(2);
  await expect(tool.first().locator('summary')).toHaveText('🔧 get_logs');
  expect(await tool.first().getAttribute('open')).toBeNull();
  await expect(tool.last().locator('pre')).toHaveText('Sep 22 dashboard started');
  await expect(page.locator('#keeper-log .keeper-msg').last()).toHaveText('Nothing is failing. <b>not html</b>');
  expect(await page.locator('#keeper-log b').count()).toBe(0);
  expect(fixture.posts.map(p => p.path)).toEqual(['keeper', 'keeper/turn']);
  expect(fixture.posts[1].body.message).toBe('Run doctor');
  await page.keyboard.press('Escape');
  await expect(page.locator('#keeper')).toBeHidden();
  await expect(page.locator('#keeper-open')).toBeFocused();
  await page.reload();
  await page.locator('#keeper-open').click();
  await expect(page.locator('#keeper-log .keeper-msg.owner')).toHaveText('Run doctor');
  await expect(page.locator('#keeper-log .keeper-msg')).toHaveCount(3);
  await expect(page.locator('#keeper-status')).toHaveText('Reply complete.');
  expect(fixture.posts).toHaveLength(2);
  await page.locator('#keeper-conversations').click();
  await expect(page.locator('#keeper-list button[aria-current="true"]')).toContainText('succeeded');
});

test('a lost turn response resumes the accepted turn after reload without re-sending', async ({ page, request, baseURL }) => {
  const fixture = await keeperFixture(page, request, baseURL, { lostTurnResponse: true });
  await summon(page);
  await page.locator('#keeper-input').fill('What is wrong?');
  await page.keyboard.press('Enter');
  await expect(page.locator('#keeper-status')).toContainText('Reopen the drawer to reconnect');
  expect(fixture.posts.map(p => p.path)).toEqual(['keeper', 'keeper/turn']);
  await page.reload();
  await page.locator('#keeper-open').click();
  await expect(page.locator('#keeper-status')).toHaveText('Reply complete.');
  await expect(page.locator('#keeper-log .keeper-msg.owner')).toHaveText('What is wrong?');
  expect(fixture.posts.map(p => p.path)).toEqual(['keeper', 'keeper/turn']);
});

test('an unreceived turn is retried with the same saved IDs', async ({ page, request, baseURL }) => {
  const fixture = await keeperFixture(page, request, baseURL, { lostBeforeTurn: true });
  await summon(page);
  await page.locator('[data-keeper-chip="What\'s wrong?"]').click();
  await expect(page.locator('#keeper-status')).toContainText('Reopen the drawer to reconnect');
  await page.locator('#keeper-x').click();
  await page.locator('#keeper-open').click();
  await expect(page.locator('#keeper-status')).toHaveText('Reply complete.');
  expect(fixture.posts.map(p => p.path)).toEqual(['keeper', 'keeper/turn', 'keeper/turn']);
  expect(fixture.posts[2].body).toEqual(fixture.posts[1].body);
});

test('Stop reports what was cancelled and a started doctor run separately', async ({ page, request, baseURL }) => {
  const fixture = await keeperFixture(page, request, baseURL, { hold: true, script: [
    { type: 'text', text: 'Running doctor now.' }, { type: 'tool_call', name: 'mcp__keeper__run_doctor', text: '{}' }] });
  await summon(page);
  await page.locator('[data-keeper-chip="Run doctor"]').click();
  await expect(page.locator('#keeper-log details.keeper-tool summary')).toHaveText('🔧 run_doctor');
  await expect(page.locator('#keeper-status')).toContainText('leaves the turn running');
  await page.locator('#keeper-stop').click();
  await expect(page.locator('#keeper-status')).toContainText('Cancelled: “Run doctor” while running run_doctor.');
  await expect(page.locator('#keeper-status')).toContainText('separate operation and keeps running on mac.tail123.ts.net');
  expect(fixture.posts.map(p => p.path)).toEqual(['keeper', 'keeper/turn', 'keeper/cancel']);
  await expect(page.locator('#keeper-send')).toBeVisible();
  await expect(page.locator('#keeper-stop')).toBeHidden();
});

test('switching graves while a turn is pending does not move it to the new target', async ({ page, request, baseURL }) => {
  await keeperFixture(page, request, baseURL, { hold: true, script: [{ type: 'text', text: 'Still thinking…' }] });
  const entryPosts = [];
  await page.route(entry + '/grave/api/v1/keeper**', route => {
    if (route.request().method() === 'POST') entryPosts.push(route.request().postDataJSON());
    return route.fulfill({ headers: cors, json: { conversations: [], provider: 'claude', model: '' } });
  });
  await summon(page);
  await page.locator('[data-keeper-chip="What\'s wrong?"]').click();
  await expect(page.locator('#keeper-log .keeper-msg.owner')).toHaveText("What's wrong?");
  await expect(page.locator('#keeper-stop')).toBeVisible();
  await page.locator('#keeper-x').click();
  await page.locator('#remote-home').click();
  await expect(page.locator('#plot-context')).not.toContainText('REMOTE-MAC');
  await page.locator('#keeper-open').click();
  await expect(page.locator('#keeper-host')).toHaveText('home.tail123.ts.net');
  await expect(page.locator('#keeper-status')).toContainText('speaks only for home.tail123.ts.net');
  await expect(page.locator('#keeper-log .keeper-msg')).toHaveCount(0);
  await expect(page.locator('#keeper-send')).toBeVisible();
  expect(entryPosts).toEqual([]);
  await summon(page);
  await expect(page.locator('#keeper-log .keeper-msg.owner')).toHaveText("What's wrong?");
  await expect(page.locator('#keeper-stop')).toBeVisible();
});

test('a failing service row summons the Keeper with a prefilled prompt', async ({ page, request, baseURL }) => {
  await keeperFixture(page, request, baseURL, { state: { services: [{ unit: 't3code.service', active: 'failed', sub: 'failed' }] } });
  await page.goto(selectedURL);
  await page.locator('[data-attention="services"]').click();
  const ask = page.locator('#services [data-keeper-ask]');
  await expect(ask).toBeVisible({ timeout: 15000 });
  await ask.click();
  await expect(page.locator('#keeper')).toBeVisible();
  await expect(page.locator('#keeper-input')).toHaveValue("Service t3code.service is failed (failed). What's wrong?");
  await expect(page.locator('#keeper-input')).toBeFocused();
  await page.locator('#keeper-x').click();
  await expect(ask).toBeFocused();
});

test('a missing provider and an interrupted turn show actionable states', async ({ page, request, baseURL }) => {
  const options = { finalState: 'failed', finalMessage: "Could not run the provider: [Errno 2] No such file or directory: 'claude'", script: [{ type: 'error', text: 'claude not found' }] };
  await keeperFixture(page, request, baseURL, options);
  await summon(page);
  await page.locator('[data-keeper-chip="Anything to update?"]').click();
  await expect(page.locator('#keeper-status')).toContainText('Install the claude CLI on mac.tail123.ts.net as the appliance owner, then send again.');
  await expect(page.locator('#keeper-log .keeper-msg.error')).toContainText('claude not found');
  options.finalState = 'interrupted'; options.finalMessage = 'Dashboard restarted during this turn; outcome unknown. Resume to ask again.';
  options.script = [{ type: 'text', text: 'Checking…' }];
  await page.locator('#keeper-input').fill('Is T3 up?');
  await page.locator('#keeper-send').click();
  await expect(page.locator('#keeper-status')).toContainText('outcome unknown. Resume to ask again. Send again to resume this session.');
  await expect(page.locator('#keeper-input')).toHaveValue('Is T3 up?');
});

test('a grave without the Keeper hides the entry and explains it in the drawer', async ({ page, request, baseURL }) => {
  await keeperFixture(page, request, baseURL);
  await page.route(remote + '/grave/api/v1/capabilities', async route => {
    const headers = { ...route.request().headers(), 'X-Forwarded-Proto': 'https', 'Tailscale-User-Login': 'browser@example.test' };
    delete headers.host;
    const response = await request.get(new URL('grave/api/v1/capabilities', baseURL).href, { headers }), data = await response.json();
    data.keeper = {}; data.routes.GET = data.routes.GET.filter(r => !r.startsWith('keeper')); data.routes.POST = data.routes.POST.filter(r => !r.startsWith('keeper'));
    return route.fulfill({ response, json: data });
  });
  await page.goto(selectedURL);
  await expect(page.locator('#plot-context')).toContainText('REMOTE-MAC');
  await expect(page.locator('#keeper-open')).toBeHidden();
  await page.evaluate(() => openKeeper(''));
  await expect(page.locator('#keeper-status')).toContainText('unavailable on mac.tail123.ts.net');
  await expect(page.locator('#keeper-send')).toBeDisabled();
});
