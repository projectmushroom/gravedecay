# Notifications — the box wakes *you*

Agents work the graveyard shift; this is how a shift ending reaches your
phone. One primitive — `grave notify` — carries everything: agent sessions
finishing, agents ringing the bell (waiting on a prompt), platform units
failing, and a failing `grave doctor`. The channel is [ntfy](https://ntfy.sh):
self-hostable, tokenizable, with iOS/Android apps and zero accounts required
on the public server.

## Setup (two steps)

1. Pick a **random, unguessable topic** (the topic name *is* the capability —
   anyone who knows it can read and publish; see Security below) and put it in
   the secret store:

   ```sh
   install -m 600 /dev/null $GRAVE_ROOT/config/secrets/notify.env
   cat >> $GRAVE_ROOT/config/secrets/notify.env <<'EOF'
   NTFY_URL=https://ntfy.sh/<your-random-topic>
   # NTFY_TOKEN=tk_...   # optional: authed / self-hosted ntfy servers
   EOF
   ```

2. Subscribe to the same topic in the ntfy app on your phone/laptop, then:

   ```sh
   grave notify "hello" "from the box"
   ```

That's it — every event source below is already wired and starts firing the
moment `notify.env` exists. `grave doctor` now verifies the channel (see
Doctor contract).

## What pages you

| Event class | Fires when | Source |
|---|---|---|
| `session-exit` | an agent tmux session ends | tmux `session-closed` hook |
| `bell` | an agent rings the terminal bell (done / waiting on a prompt) | tmux `alert-bell` hook |
| `unit-failure` | a platform unit enters failed state (t3code, dashboard, terminal, gateway, upgrades, gamewatch, selfheal) | `OnFailure=gravedecay-notify@%n` |
| `doctor` | a `grave doctor` run has failing checks | doctor itself |

Mute a class by removing its word from `NOTIFY_EVENTS` in
`/etc/gravedecay/grave.conf`; muting all four leaves `grave notify` available
for manual/scripted use (`grave agents new` sessions can call it as the last
command of a long job, for example: `long-build; grave notify "build done"`).

Every source calls `grave notify --event <class>`, which is a **silent no-op**
when the channel is unconfigured or the class is muted — the hooks and
`OnFailure=` lines ship enabled on every box and cost nothing until you opt
in.

## Existing boxes

`raise.sh` installs the `gravedecay-notify@.service` unit and refreshed unit
files on the next re-raise, but it deliberately never clobbers an existing
`config/tmux.conf` — re-copy it from the repo to pick up the session
hooks (same drill as the clipboard config):

```sh
cp $GRAVE_ROOT/repos/gravedecay/config/tmux.conf $GRAVE_ROOT/config/tmux.conf
```

Running tmux servers read hooks at session creation; restart the agents server
(`grave gaming --kill` + new sessions, or a reboot) for the hooks to load.

## Security

- **The topic is a capability.** On public ntfy.sh anyone who guesses the
  topic can read and send; use a long random name
  (`python3 -c 'import secrets; print(secrets.token_urlsafe(16))'`), or run a
  self-hosted/authed server and set `NTFY_TOKEN`.
- The config lives in the secret store (`config/secrets/`, 600, git-ignored,
  excluded from backups unless `--include-secrets`) like every other secret —
  see `docs/SECRETS.md`.
- Notification *content* is deliberately terse (session name, unit name,
  check count) — no log lines, no repo contents, nothing you'd mind a leaked
  topic exposing.
- Session names cross a header boundary on the way out; `grave notify` strips
  CR/LF from the title and the tmux hooks shell-quote names with `#{q:...}`,
  so a hostile session name can't inject headers or shell.
- Outbound HTTPS only — no new listening port, so no `docs/PORTS.md` entry.

## Doctor contract

When `config/secrets/notify.env` exists, doctor enforces:

- **notify channel reachable** — polls the topic (`?poll=1`, never publishes)
  through the configured auth, because a dead channel silently eats every
  page.
- **notify secret is private** — `notify.env` is mode 600.
- **failure notifier installed** — `gravedecay-notify@.service` exists.

A failing doctor run is itself a `doctor`-class notification, so a broken
invariant pages you even when you aren't looking at the dashboard.
