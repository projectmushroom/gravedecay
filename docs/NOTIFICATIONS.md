# Notifications — the box wakes *you*

Agents work the graveyard shift; this is how a shift ending reaches your
phone. One primitive — `grave notify` — fans out over two channels, use
either or both:

- **Web Push** to the installed gravedecay PWA — native notifications on
  iPhone/iPad/Android/desktop, no third-party service, no extra app. The
  dashboard holds the VAPID key and does the RFC 8291 crypto; a tap on the
  notification deep-links back into the app (a session's terminal, the System
  tab).
- **[ntfy](https://ntfy.sh)** — for devices without the PWA, watch-face
  mirroring, or scripting; self-hostable and tokenizable.

Everything below fires through both: agent sessions finishing, agents ringing
the bell (waiting on a prompt), platform units failing, a failing
`grave doctor`.

## Setup

### Web Push (recommended)

1. Install the PWA from `https://<box>.<tailnet>.ts.net/grave/` (iOS: Share →
   Add to Home Screen — push requires iOS 16.4+ and the *installed* app, not
   the Safari tab).
2. In the app: ⚙️ settings → **Notifications** → **🔔 enable**, and accept the
   permission prompt.
3. **📣 send test**. Repeat step 2 on each device (they all appear in the
   device list; ✕ removes one).

Requires `python3-cryptography` on the box — `raise.sh` installs it
(best-effort via pip on SteamOS); without it the settings panel says so and
ntfy is unaffected.

### ntfy

Pick a **random, unguessable topic** (the topic name *is* the capability —
anyone who knows it can read and publish; see Security) and either paste it
into ⚙️ settings → Notifications → **ntfy URL**, or put it in the secret
store by hand:

```sh
install -m 600 /dev/null $GRAVE_ROOT/config/secrets/notify.env
cat >> $GRAVE_ROOT/config/secrets/notify.env <<'EOF'
NTFY_URL=https://ntfy.sh/<your-random-topic>
# NTFY_TOKEN=tk_...   # optional: authed / self-hosted ntfy servers
EOF
```

Subscribe to the topic in the ntfy app, then `grave notify "hello"`.

## What pages you

| Event class | Fires when | Source | Tap opens |
|---|---|---|---|
| `session-exit` | an agent tmux session ends | tmux `session-closed` hook | `/term/` |
| `bell` | an agent rings the terminal bell (done / waiting on a prompt) | tmux `alert-bell` hook | that session's terminal |
| `unit-failure` | a platform unit enters failed state (t3code, dashboard, terminal, gateway, upgrades, gamewatch, selfheal) | `OnFailure=gravedecay-notify@%n` | System tab |
| `doctor` | a `grave doctor` run has failing checks | doctor itself | System tab |

Mute a class with the checkboxes in ⚙️ settings → Notifications (written to
`$GRAVE_ROOT/config/notify-events`, which overrides `NOTIFY_EVENTS` in
grave.conf — the dashboard runs unprivileged, so a preference flip must not
need sudo). Muting all four leaves `grave notify` available for
manual/scripted use: `long-build; grave notify "build done"`.

Every source calls `grave notify --event <class>`, a **silent no-op** when no
channel is configured or the class is muted — the hooks and `OnFailure=`
lines ship enabled on every box and cost nothing until you opt in.

## How Web Push works here

- The dashboard generates a **VAPID P-256 key** on first use
  (`config/secrets/vapid.pem`, 600). Rotating/deleting it orphans every
  subscription — devices just re-enable.
- Enrolling stores the browser's push subscription in
  `config/push-subscriptions.json` (600, capped at 10 devices). The endpoint
  is a push-service capability URL and **never leaves the box** — the UI sees
  an opaque id.
- `grave notify` POSTs the message to the dashboard's loopback
  `/api/push-send`; the dashboard encrypts per **RFC 8291** (aes128gcm) and
  signs per **RFC 8292** (VAPID ES256) — pinned by the RFC's own test vector
  in `tests/test_push.py` — then delivers to each device's push service.
  Payloads are end-to-end encrypted; Apple/Google relays see ciphertext.
- Dead subscriptions (404/410 — permission revoked, PWA reinstalled) are
  pruned automatically on the next send.
- `gravedecay.service` is ALWAYS_ON, so pushes deliver even in gaming mode.

## Existing boxes

`raise.sh` installs the `gravedecay-notify@.service` unit and refreshed unit
files on the next re-raise, but it deliberately never clobbers an existing
`config/tmux.conf` — re-copy it from the repo to pick up the session hooks
(same drill as the clipboard config):

```sh
cp $GRAVE_ROOT/repos/gravedecay/config/tmux.conf $GRAVE_ROOT/config/tmux.conf
```

Running tmux servers read hooks at session creation; restart the agents
server (`grave gaming --kill` + new sessions, or a reboot) for the hooks to
load.

## Security

- **The ntfy topic is a capability.** On public ntfy.sh anyone who guesses it
  can read and send; use a long random name
  (`python3 -c 'import secrets; print(secrets.token_urlsafe(16))'`), or a
  self-hosted/authed server with `NTFY_TOKEN`. Web Push has no such caveat —
  payloads are encrypted to each device.
- Channel secrets live in the secret store (`config/secrets/`, 600,
  git-ignored, excluded from backups unless `--include-secrets`); the
  subscription store is 600 beside the other appliance config.
- Notification *content* is deliberately terse (session name, unit name,
  check count) — no log lines, no repo contents.
- Session names cross shell, header, and JSON boundaries on the way out: tmux
  hooks quote with `#{q:...}`, `grave notify` strips CR/LF from ntfy headers
  and builds the push payload with `jq`, and the dashboard drops any deep
  link that isn't an on-origin `/path`.
- Enrollment endpoints (`/api/push-key`, `/api/push-subscribe`, …) are gated
  to ALLOWED_USERS + CSRF-checked like every other privileged route.
- Outbound HTTPS only — no new listening port, so no `docs/PORTS.md` entry.

## Doctor contract

Gated on each channel being opted into:

- **ntfy channel reachable** (`notify.env` exists) — polls the topic
  (`?poll=1`, never publishes) through the configured auth.
- **ntfy secret is private** — `notify.env` is mode 600.
- **push subscription store / VAPID key private** (devices enrolled) — both
  mode 600.
- **web push sender ready** — asks the dashboard's `/api/push-key`, catching
  a `cryptography` module that vanished (e.g. after an OS update).
- **failure notifier installed** (either channel) —
  `gravedecay-notify@.service` exists.

A failing doctor run is itself a `doctor`-class notification, so a broken
invariant pages you even when you aren't looking at the dashboard.
