# The web terminal (`/term`)

ttyd on loopback `:4713`, exposed at `https://<box>.ts.net/term` by
`tailscale serve`. Every session is a window in the shared `tmux -L agents`
socket (`bin/webterm` picks the session from `?arg=`), so the browser, SSH,
and `grave agents attach` all reach the same persistent shell.

## Why a custom frontend

ttyd serves a single built-in page. The packaged release (1.7.7, March 2024)
predates ttyd's OSC 52 clipboard support (added to master Nov 2024, never
released) — so the escape sequence tmux emits on every mouse copy
(`set-clipboard on` + the `Ms` override in `config/tmux.conf`) was silently
dropped, and **copy out of the terminal didn't work at all** (issue #104).
On touch devices it was worse: xterm.js has no touch selection, so a phone
had no copy path whatsoever.

`raise.sh` therefore builds a single-file frontend from `web/term/` and runs
ttyd with `-I $GRAVE_ROOT/web/term/index.html`:

- `web/term/app.js` — our client. Speaks the ttyd 1.7.7 websocket protocol
  (auth token, input/resize, pause/resume flow control) and adds:
  - an **OSC 52 handler**: tmux mouse-copy lands on the system clipboard.
    Clipboard *reads* via OSC 52 (`?`) are never answered — that would leak
    the clipboard to anything running in the terminal.
  - **copy on select** (also catches Shift+drag native selections),
  - a **📋/📥 button bar**: WebKit only allows clipboard writes inside a user
    gesture and OSC 52 arrives async, so a rejected auto-write arms the 📋
    button and one real tap completes the copy. 📥 pastes (with a manual
    textarea fallback where the browser refuses clipboard reads).
  - Ctrl/Cmd+Shift+C / V.
- `web/term/index.tmpl.html` — page skeleton; carries the
  `gravedecay-term-frontend` marker `grave doctor` greps for.
- `web/term/vendor/` — pinned xterm.js, committed verbatim from npm:
  `@xterm/xterm@5.5.0` (tarball sha256
  `bd954fa721872170188cc5d7e83e88db3c83c9a18a4e8d24c2783d26491f59d2`) and
  `@xterm/addon-fit@0.10.0` (sha256
  `917ac44972453d5eed52edc1e50260c76398ce48cf2290c2e60671102bba0b33`).
  To upgrade: fetch the new tarballs, record the hashes here, rerun
  `./raise.sh`.

The build is a pure text splice (no network, no node) done by `raise.sh` —
rerun it after changing anything under `web/term/`. Never edit the installed
`$GRAVE_ROOT/web/term/index.html`; doctor pins the contract:

- `web terminal answering` — ttyd is up,
- `web terminal clipboard frontend` — the served index is ours.

## Every way to copy

| Where | How |
|---|---|
| Desktop, mouse | drag to select (tmux copy-mode) — lands on the clipboard via OSC 52; or Shift+drag for a native browser selection |
| Any | select, then Ctrl/Cmd+Shift+C; or the 📋 button (selection → last copy → visible screen) |
| Phone / touch / iOS PWA | dashboard → 🤖 Agent sessions → 📋 next to the session: last 2000 scrollback lines in a textarea (native selection works there), plus a copy-all button |

Paste: 📥 button or Ctrl/Cmd+Shift+V. Firefox blocks programmatic clipboard
reads — the 📥 button falls back to a paste-here textarea.

Scrollback stays tmux's job (`history-limit 100000` in `config/tmux.conf`);
the frontend runs xterm.js with `scrollback: 0` so the mouse wheel scrolls
tmux copy-mode, not a second divergent buffer.
