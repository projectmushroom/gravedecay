// Digger M2: the Work tab's Diggers section and the drawer bound to one digger (docs/DASHBOARD.md).
const { test } = require('@playwright/test');
const expect = require('@playwright/test').expect.configure({ timeout: 10000 });
test.use({ serviceWorkers: 'block' });
test.afterEach(async ({ page }) => page.unrouteAll({ behavior: 'ignoreErrors' }));

const rows = [
  { name: 'wl', provider: 'codex', model: '', identity: 'dedicated', repo: 'app', enabled: true, live: true, paused: false,
    loggedIn: true, queue: 0, running: null, today: { turns: 3, usd: 0.42 }, last_verdict: 'ready-for-review',
    prompts: [{ label: 'New white label', template: 'Create white label {name} with domain {domain}' }] },
  { name: 'docs', provider: 'claude', model: '', identity: 'separate-login', repo: 'app', enabled: false, live: false, paused: false,
    loggedIn: false, queue: 0, running: null, today: { turns: 0, usd: 0 }, last_verdict: null, prompts: [] },
];
const result = { verdict: 'ready-for-review', changes: { commits: 1, files: 1, insertions: 1, deletions: 0, dirty: false },
  checks: [{ cmd: 'test -f config/whitelabels/acme.json', exit_code: 0, seconds: 0.1, tail: '', source: 'owner' }],
  cost: { usd: 0.05, estimated: true, model: 'gpt-5-codex' } };

// A scripted digger: one event per cursor read, then the turn settles (and,
// with options.finish, the conversation is packaged into a result).
async function diggerFixture(page, request, baseURL, options = {}) {
  const state = await (await request.get(new URL('api/state', baseURL).href)).json();
  Object.assign(state, { mode: 'developer', tmux: [], agent_history: [], scheduled: { available: true, jobs: [], runs: [] } });
  state.settings = { ...state.settings, hidden_panels: [], poll_ms: 30000 };
  const source = await (await request.get(baseURL)).text();
  await page.route('**/api/state', route => route.fulfill({ json: state }));
  await page.route('**/api/t3-activity', route => route.fulfill({ json: { status: 'ok', threads: [] } }));
  await page.route('**/*', route => route.request().resourceType() === 'document'
    ? route.fulfill({ contentType: 'text/html', body: source.replace(/const BOOT=[^\n]+/, () => `const BOOT=${JSON.stringify(state).replaceAll('<', '\\u003c')};`) })
    : route.fallback());
  await page.route('**/api/v1/capabilities', async route => {
    const json = await (await route.fetch()).json();
    for (const name of ['diggers', 'diggers/conversation']) if (!json.routes.GET.includes(name)) json.routes.GET.push(name);
    for (const name of ['diggers/ask', 'diggers/cancel', 'diggers/finish']) if (!json.routes.POST.includes(name)) json.routes.POST.push(name);
    return route.fulfill({ json });
  });
  const posts = []; let record = null;
  const view = after => { const { pending, ...rest } = record; return { ...rest, events: record.events.filter(e => e.seq > after) }; };
  await page.route('**/api/digger-diff**', route => route.fulfill({ contentType: 'text/plain', body: '+{"name": "acme"}\n' }));
  await page.route('**/api/v1/diggers**', async route => {
    const req = route.request(), url = new URL(req.url()), path = url.pathname.replace(/^.*\/api\/v1\//, '');
    if (req.method() === 'POST') {
      const body = req.postDataJSON(); posts.push({ path, body });
      if (path === 'diggers/cancel') {
        if (record?.state === 'running') Object.assign(record, { state: 'cancelled', message: 'Cancelled by the owner', pending: [] });
        return route.fulfill({ json: view(0) });
      }
      if (options.queued) {
        record = { id: body.conversation, digger: body.name, state: 'queued', phase: 'open', events: [], queued: 1, turn: null,
          message: '', result: null, live: false, pending: [] };
        return route.fulfill({ status: 202, json: { ...view(0), request: body.request } });
      }
      record = record || { id: body.conversation, digger: body.name, provider: 'codex', model: '', session_id: null, host: 'box',
        owner: 'digger', created_at: 1, updated_at: 1, summary: '', state: 'idle', turn: null, cursor: 0, events: [],
        truncated: false, message: '', phase: 'open', settling: false, queued: 0, checkout: '/srv/dev/diggers/wl/worktrees/x',
        branch: 'digger/wl/' + body.conversation, base: 'a'.repeat(40), result: null, requested_by: 'owner' };
      Object.assign(record, { state: 'running', turn: { id: body.request, started_at: 2, finished_at: null, exit_code: null } });
      record.events.push({ seq: ++record.cursor, type: 'text', name: 'owner', text: body.message, at: 2 });
      record.pending = (options.script || [
        { type: 'tool_call', name: 'wl__create', text: '{"name":"acme"}' },
        { type: 'text', text: 'Created acme. <b>not html</b>\n[[digger:done]]' }]).slice();
      return route.fulfill({ status: 202, json: { ...view(0), request: body.request } });
    }
    if (path === 'diggers' && !url.searchParams.get('name')) return route.fulfill({ json: { diggers: rows, invalid: [{ name: 'broken', error: 'unknown key(s): sandbox' }] } });
    if (path === 'diggers') return route.fulfill({ json: { conversations: record ? [{ id: record.id, state: record.state, phase: record.phase,
      summary: 'Created acme.', requested_by: 'owner', created_at: 1, updated_at: 1, verdict: record.result?.verdict || null }] : [] } });
    if (!record || record.id !== url.searchParams.get('id')) return route.fulfill({ status: 404, json: { error: 'not_found', output: 'No such conversation' } });
    if (record.state === 'running' && record.pending.length) {
      record.events.push({ seq: ++record.cursor, name: '', ...record.pending.shift(), at: 3 });
      if (!record.pending.length && !options.hold) {
        Object.assign(record, { state: 'succeeded', summary: 'Created acme.' });
        record.turn.finished_at = 4;
        if (options.finish !== false) Object.assign(record, { phase: 'finished', result, result_status: 'succeeded', finished_at: 5 });
      }
    }
    return route.fulfill({ json: view(Number(url.searchParams.get('after') || 0)) });
  });
  await page.goto(new URL('?tab=work', baseURL).href);
  return { posts, get record() { return record; } };
}

async function summon(page, name = 'wl') {
  const panel = page.locator('[data-panel="diggers"]');
  await expect(panel).toBeVisible();
  if (await panel.locator('.panel-toggle').getAttribute('aria-expanded') !== 'true') await panel.locator('.panel-toggle').click();
  await page.locator(`[data-digger-summon="${name}"]`).click();
  await expect(page.locator('#keeper')).toBeVisible();
  await expect(page.locator('#keeper-title')).toHaveText(`⛏️ ${name}`);
}

test('the section lists each digger with its tier, state and spend', async ({ page, request, baseURL }) => {
  await diggerFixture(page, request, baseURL);
  const panel = page.locator('[data-panel="diggers"]');
  await expect(panel).toBeVisible();
  await expect(panel.locator('.panel-summary')).toHaveText('2 diggers · 1 invalid');
  if (await panel.locator('.panel-toggle').getAttribute('aria-expanded') !== 'true') await panel.locator('.panel-toggle').click();
  const wl = panel.locator('[data-digger="wl"]');
  await expect(wl).toContainText('dedicated · codex · app');
  await expect(wl).toContainText('idle · logged in · 0 queued requests · today 3 turns, $0.42 · last: ready-for-review');
  await expect(panel.locator('[data-digger="docs"]')).toContainText('disabled · not logged in');
  await expect(page.locator('[data-digger-summon="docs"]')).toBeDisabled();
  await expect(panel).toContainText('unknown key(s): sandbox');
});

test('a request streams text-only events and ends with the result card and its diff', async ({ page, request, baseURL }) => {
  const fixture = await diggerFixture(page, request, baseURL);
  await summon(page);
  await expect(page.locator('#keeper-chips')).toBeHidden();
  await expect(page.locator('#keeper-status')).toContainText('codex · dedicated · works in app');
  await page.locator('#keeper-input').fill('create white label acme');
  await page.keyboard.press('Enter');
  await expect(page.locator('#keeper-log .keeper-msg.owner')).toHaveText('create white label acme');
  await expect(page.locator('#keeper-log details.keeper-tool summary')).toHaveText('🔧 wl__create');
  await expect(page.locator('#keeper-log .keeper-msg').last()).toContainText('Created acme. <b>not html</b>');
  expect(await page.locator('#keeper-log .keeper-msg b').count()).toBe(0);
  const card = page.locator('#keeper-log .digger-result .runcard');
  await expect(card).toHaveAttribute('data-verdict', 'ready-for-review');
  await expect(card).toContainText('1 commit · 1 file · +1 −0');
  await expect(page.locator('#keeper-status')).toContainText('Finished: ready-for-review.');
  const ask = fixture.posts.find(p => p.path === 'diggers/ask').body;
  expect(Object.keys(ask).sort()).toEqual(['conversation', 'message', 'name', 'request']);
  expect(ask.name).toBe('wl');
  await card.locator('summary').click();
  await card.locator('a.viewdiff').click();
  await expect(page.locator('#log-text')).toHaveText('+{"name": "acme"}');
});

test('Stop posts a cancel for the running conversation', async ({ page, request, baseURL }) => {
  const fixture = await diggerFixture(page, request, baseURL, { hold: true, script: [{ type: 'text', text: 'Digging…' }] });
  await summon(page);
  await page.locator('#keeper-input').fill('sleep on it');
  await page.keyboard.press('Enter');
  await expect(page.locator('#keeper-status')).toContainText('wl is working');
  await page.locator('#keeper-stop').click();
  await expect(page.locator('#keeper-status')).toContainText('Stopped. Cancelled by the owner');
  const cancel = fixture.posts.find(p => p.path === 'diggers/cancel').body;
  expect(cancel.conversation).toBe(fixture.record.id);
  await expect(page.locator('#keeper-send')).toBeVisible();
});

test('a prompt chip with fields asks for them and sends the filled template', async ({ page, request, baseURL }) => {
  const fixture = await diggerFixture(page, request, baseURL, { finish: false });
  await summon(page);
  await page.locator('[data-digger-chip="New white label"]').click();
  const form = page.locator('#digger-fields');
  await expect(form).toBeVisible();
  await form.locator('input[name="name"]').fill('acme');
  await form.locator('input[name="domain"]').fill('acme.example');
  await form.locator('button[type="submit"]').click();
  await expect(page.locator('#keeper-log .keeper-msg.owner')).toHaveText('Create white label acme with domain acme.example');
  await expect(page.locator('#keeper-status')).toContainText('Answer to continue, or Finish');
  await expect(page.locator('#digger-finish')).toBeVisible();
  expect(fixture.posts[0].body.message).toBe('Create white label acme with domain acme.example');
});

test('a queued request on a stopped digger says so', async ({ page, request, baseURL }) => {
  await diggerFixture(page, request, baseURL, { queued: true });
  await summon(page);
  await page.locator('#keeper-input').fill('hello');
  await page.keyboard.press('Enter');
  await expect(page.locator('#keeper-status')).toContainText('Queued, but wl is not running');
  await expect(page.locator('#keeper-status')).toContainText('grave digger enable wl');
});
