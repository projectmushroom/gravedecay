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
| Self-updater | `gravedecay-upgrade.service`, detached oneshot | Survives the dashboard restart caused by its own re-raise |

In opt-in multi-user mode, Tailscale Serve sends the whole HTTPS origin to the
identity gateway on loopback :4710 using a root-only random capability path.
The gateway confirms the Serve identity against local Tailscale state, resolves
the stable ID in the workspace registry, enforces the role, and selects fixed
T3, terminal, and dashboard ports. User input never selects an upstream. The
proxy supports ordinary HTTP streaming and WebSocket upgrades by relaying the
connection bidirectionally.

Each workspace has three templated services running as `grave-<slug>`: T3,
ttyd/tmux, and a workspace-scoped dashboard. HOME, XDG state/config/runtime,
repos, logs, ports, and tmux socket names are private. Units use loopback-only
listeners, restrictive umasks, `NoNewPrivileges`, and CPU/task/memory ceilings.
Stopping or restarting one instance addresses only that workspace; enabled
instances participate in boot and developer/gaming transitions.

Project grants are registry records, not shared filesystem permissions. A grant
clones into that workspace's private `repos/<project>` as its Unix identity;
two collaborators therefore have independent indexes, branches, remotes, and
dirty files. Revocation removes the checkout from T3/dashboard visibility at
once by moving it into the same private workspace's `revoked/` retention tree.
It never deletes dirty or unpushed work. Git identity and signing configuration
are written to that workspace user's own global Git configuration.

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

Dashboard self-upgrades are queued with `systemctl --no-block` into either
`gravedecay-upgrade.service` (configured release/edge channel) or the validated
`gravedecay-upgrade@vX.Y.Z.service` instance selected in the release picker.
They must never execute as a child of the dashboard: `grave upgrade` invokes
`raise.sh`, which restarts `gravedecay.service` and kills that service's
remaining cgroup processes. Both paths refuse to touch a checkout with
uncommitted changes; explicit tags must exist after fetching the configured
repository.

Every raise explicitly restarts the dashboard, terminal, T3, gateway, and
workspace services after installing their scripts and unit files. Merely using
`systemctl enable --now` is insufficient because systemd does not restart an
already-active unit. Dashboard health reports the running source hash and
`grave doctor` compares it with the installed script, making stale processes a
visible contract failure instead of silently serving an old UI.
