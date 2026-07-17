'use strict';
// gravedecay-term-frontend — custom ttyd index (docs/TERMINAL.md, issue #104).
// Speaks the ttyd 1.7.7 websocket protocol and adds what the stock 1.7.7
// frontend is missing: an OSC 52 handler (tmux `set-clipboard on` emits it on
// every mouse copy — the packaged ttyd release silently drops it), copy on
// select, and an on-screen copy/paste bar. The bar matters beyond convenience:
// WebKit only allows clipboard writes inside a user gesture, and OSC 52
// arrives async over the websocket, so the auto-write can be rejected — the
// payload is kept in `lastCopy` and the armed 📋 button retries with a real
// tap. tmux owns scrollback/mouse; full-history copy lives in the dashboard
// (session 📋 → tmux capture-pane).

/* global Terminal, FitAddon */
(() => {
  // ---- ttyd protocol (client → server first byte) ----
  const INPUT = '0', RESIZE = '1', PAUSE = '2', RESUME = '3';
  // server → client first byte: '0' output, '1' title, '2' preferences
  const FLOW = { limit: 100000, highWater: 10, lowWater: 4 }; // stock values

  const enc = new TextEncoder(), dec = new TextDecoder();
  const $ = id => document.getElementById(id);

  const term = new Terminal({
    fontSize: 14,
    fontFamily: 'ui-monospace, SFMono-Regular, Menlo, Consolas, monospace',
    theme: { background: '#0d0d0d' },
    cursorBlink: true,
    allowProposedApi: true,   // parser.registerOscHandler needs it
    scrollback: 0,            // tmux owns scrollback (mouse wheel enters copy-mode)
  });
  const fit = new FitAddon.FitAddon();
  term.loadAddon(fit);
  term.open($('terminal'));
  fit.fit();
  term.focus();

  // ---- clipboard ----
  let lastCopy = '';          // last OSC 52 payload / selection, for the 📋 button

  function toast(msg) {
    const t = $('toast');
    t.textContent = msg;
    t.classList.add('show');
    clearTimeout(toast.timer);
    toast.timer = setTimeout(() => t.classList.remove('show'), 1400);
  }
  function armCopy() {        // auto-write rejected: ask for one real tap
    $('copy').classList.add('armed');
    toast('📋 tap to copy');
  }
  function disarmCopy() { $('copy').classList.remove('armed'); }

  function writeClipboard(text) {
    if (!text) return Promise.resolve(false);
    lastCopy = text;
    const done = ok => { if (ok) { disarmCopy(); toast('📋 copied'); } else armCopy(); return ok; };
    if (navigator.clipboard && navigator.clipboard.writeText)
      return navigator.clipboard.writeText(text).then(() => done(true), () => done(legacyCopy(text)));
    return Promise.resolve(done(legacyCopy(text)));
  }
  function legacyCopy(text) { // plain-http localhost has no async clipboard API
    const ta = document.createElement('textarea');
    ta.value = text;
    ta.style.cssText = 'position:fixed;opacity:0';
    document.body.appendChild(ta);
    ta.select();
    let ok = false;
    try { ok = document.execCommand('copy'); } catch (_) { /* fall through */ }
    ta.remove();
    term.focus();
    return ok;
  }

  // OSC 52: tmux (Ms capability) sends "c;<base64>" after every copy-mode
  // copy — decode and forward to the system clipboard. "?" asks to READ the
  // clipboard; never answer that (it would leak the clipboard to anything
  // running in the terminal).
  term.parser.registerOscHandler(52, payload => {
    const b64 = payload.slice(payload.indexOf(';') + 1);
    if (b64 === '?') return true;
    try {
      writeClipboard(dec.decode(Uint8Array.from(atob(b64), c => c.charCodeAt(0))));
    } catch (_) { /* malformed base64: ignore */ }
    return true;
  });

  // Copy on select: Shift+drag (native tmux-mouse bypass) lands here too.
  term.onSelectionChange(() => {
    const sel = term.getSelection();
    if (sel) writeClipboard(sel);
  });

  function visibleScreen() {  // 📋 with nothing selected: grab what's on screen
    const buf = term.buffer.active, lines = [];
    for (let i = 0; i < term.rows; i++) {
      const line = buf.getLine(buf.viewportY + i);
      lines.push(line ? line.translateToString(true) : '');
    }
    return lines.join('\n').replace(/\n+$/, '\n');
  }

  function doPaste() {
    if (navigator.clipboard && navigator.clipboard.readText)
      navigator.clipboard.readText().then(t => { term.paste(t); term.focus(); },
                                          () => $('paste-dlg').hidden = false);
    else $('paste-dlg').hidden = false; // Firefox denies readText: manual box
  }

  $('copy').addEventListener('click', () => {
    writeClipboard(term.getSelection() || lastCopy || visibleScreen());
    term.focus();
  });
  $('paste').addEventListener('click', doPaste);
  $('paste-send').addEventListener('click', () => {
    term.paste($('paste-ta').value);
    $('paste-ta').value = '';
    $('paste-dlg').hidden = true;
    term.focus();
  });
  $('paste-x').addEventListener('click', () => { $('paste-dlg').hidden = true; term.focus(); });

  term.attachCustomKeyEventHandler(e => {
    if (e.type !== 'keydown' || !e.shiftKey || !(e.ctrlKey || e.metaKey)) return true;
    if (e.code === 'KeyC' && term.hasSelection()) { writeClipboard(term.getSelection()); return false; }
    if (e.code === 'KeyV') { doPaste(); return false; }
    return true;
  });

  // ---- websocket (stock ttyd URL scheme; ?arg= must survive into /ws) ----
  const path = window.location.pathname.replace(/[/]+$/, '');
  const wsUrl = (window.location.protocol === 'https:' ? 'wss:' : 'ws:') +
    '//' + window.location.host + path + '/ws' + window.location.search;
  const tokenUrl = window.location.protocol + '//' + window.location.host + path + '/token';

  let ws = null, opened = false, retry = 0;
  let written = 0, pending = 0, paused = false;

  function status(msg) {
    const s = $('status');
    if (!msg) { s.hidden = true; return; }
    s.textContent = msg;
    s.hidden = false;
  }

  function send(data) {
    if (ws && ws.readyState === WebSocket.OPEN) ws.send(enc.encode(data));
  }

  function connect() {
    status(retry ? `reconnecting… (${retry})` : 'connecting…');
    fetch(tokenUrl).then(r => r.json()).catch(() => ({ token: '' })).then(({ token }) => {
      ws = new WebSocket(wsUrl, ['tty']);
      ws.binaryType = 'arraybuffer';
      ws.onopen = () => {
        opened = true; retry = 0;
        written = 0; pending = 0; paused = false;
        status('');
        // tmux new-session -A reattaches, but the old screen content is
        // stale after a reconnect — clear before the server repaints
        term.reset();
        ws.send(enc.encode(JSON.stringify(
          { AuthToken: token || '', columns: term.cols, rows: term.rows })));
      };
      ws.onmessage = ev => {
        const raw = new Uint8Array(ev.data);
        const cmd = String.fromCharCode(raw[0]), data = raw.subarray(1);
        if (cmd === '0') {                       // OUTPUT (+ flow control)
          written += data.length;
          if (written > FLOW.limit) {
            written = 0; pending++;
            if (pending > FLOW.highWater && !paused) { paused = true; send(PAUSE); }
            term.write(data, () => {
              pending--;
              if (paused && pending < FLOW.lowWater) { paused = false; send(RESUME); }
            });
          } else term.write(data);
        } else if (cmd === '1') {                // SET_WINDOW_TITLE
          if (!connect.titleFixed) document.title = dec.decode(data);
        } else if (cmd === '2') {                // SET_PREFERENCES (-t flags)
          let prefs = {};
          try { prefs = JSON.parse(dec.decode(data)); } catch (_) { /* ignore */ }
          for (const k of ['fontSize', 'fontFamily', 'theme', 'cursorBlink'])
            if (prefs[k] !== undefined) term.options[k] = prefs[k];
          if (prefs.titleFixed) { connect.titleFixed = true; document.title = prefs.titleFixed; }
          if (prefs.theme && prefs.theme.background)
            document.body.style.background = prefs.theme.background;
          fit.fit();
        }
      };
      ws.onclose = () => {
        ws = null;
        // the session survives in tmux — reconnect forever, backing off to 10s
        const delay = opened && !retry ? 300 : Math.min(1000 * 2 ** retry, 10000);
        opened = false; retry++;
        status(`disconnected — reconnecting… (${retry})`);
        setTimeout(() => { if (!ws) connect(); }, delay);
      };
    });
  }

  term.onData(d => send(INPUT + d));
  term.onBinary(d => send(INPUT + d));
  term.onResize(({ cols, rows }) => send(RESIZE + JSON.stringify({ columns: cols, rows: rows })));

  const refit = () => fit.fit();
  window.addEventListener('resize', refit);
  if (window.visualViewport)  // mobile keyboard show/hide resizes the viewport
    window.visualViewport.addEventListener('resize', refit);
  document.addEventListener('visibilitychange', () => {
    if (!document.hidden && !ws) connect();
  });

  connect();
})();
