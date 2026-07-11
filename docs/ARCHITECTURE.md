# Architecture

## The one idea

Coding agents need real processes, real files, and real builds — so everything
an agent touches runs **natively on the host**. Docker exists only for backing
services your projects need (databases, browsers). This is the opposite of
"containerize everything," on purpose.

## Layers

| Layer | Runs as | Why |
|---|---|---|
| T3 Code (web UI) | `t3code.service`, loopback :4711 | Spawns `claude`/`codex` CLI sessions as host child processes |
| gravedecay | `gravedecay.service`, loopback :4712 | Needs systemd/journald/tmux/sysfs — impossible from a container |
| CLI agent sessions | `tmux -L agents` | Survive client disconnects; `grave agents new/attach` |
| Backing services | Docker compose stacks in `$GRAVE_ROOT/docker/` | Postgres, Redis, Playwright — disposable, loopback-bound |
| Control plane | `grave` (bash, `/usr/local/bin`) | One entrypoint for modes, doctor, logs, backup |

## Filesystem

Everything lives under `$GRAVE_ROOT` (default `/srv/dev`):

```
repos/     all git checkouts (~/Projects symlinks here)
agents/    per-agent state: t3code server state, tmux session logs
docker/    compose stacks (core, browsers, yours)
config/    grave.conf source-of-truth copies, tmux.conf, secrets/ (600, git-ignored)
scripts/   gravedecay.py, dashboard-static/ PWA shell assets, and helpers
logs/      grave.log
backups/   timestamped: git bundles + config tars + volume tars
docs/      this documentation, synced from the repo
```

On btrfs, put `$GRAVE_ROOT` on its own subvolume with hourly snapper
snapshots, and `/var/lib/docker` on a separate never-snapshotted subvolume.

## Modes

- **developer** — everything up.
- **gaming** — `grave gaming` stops DEV_SERVICES, agent tmux server, compose
  stacks (reverse order), then docker itself. ALWAYS_ON services (tailscaled,
  sshd, NetworkManager, gravedecay, profile-added units) are never touched, so
  the box stays reachable and observable mid-game.

Mode is not stored anywhere: it is *derived* (t3code active ⇒ developer).
No state files to drift.

## The doctor contract

Every platform invariant is a `grave doctor` check. If a profile or a manual
tweak establishes something new ("GPU must be pinned", "docker on its own
subvolume"), it must add a check (via the `CHECK_*` flags or a profile edit) —
an invariant doctor can't see will silently regress.

## Dashboard PWA boundary

The installed gravedecay web app owns the entire Tailscale Serve HTTPS origin,
not only `/grave/`. Its manifest starts at `/grave/` but declares scope `/` so
launcher navigation to T3 (`/`), the terminal (`/term/`), and pairing (`/pair/`)
stays in one standalone iOS, iPadOS, or macOS Safari app.

The dashboard is network-first because it controls a remote machine. API
responses, machine state, file listings, and action output are always
`no-store`. A service worker caches only a static connection-help page so a
disconnected launch explains how to restore Tailscale instead of showing a
blank browser error. `grave doctor` verifies the manifest scope and root-scoped
service worker contract.
