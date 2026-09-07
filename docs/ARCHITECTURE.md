# Architecture

## The one idea

The full appliance runs everything an agent touches **natively on the host**;
Docker there is only for backing services your projects need (databases,
browsers). A separate [`docker/portable`](DOCKER.md) deployment is intentionally
smaller: a self-contained work-plane, not an appliance control plane.

## Layers

| Layer | Runs as | Why |
|---|---|---|
| T3 Code (web UI) | `t3code.service`, loopback :4711 | Spawns `claude`/`codex` CLI sessions as host child processes |
| gravedecay | `gravedecay.service`, loopback :4712 | Needs systemd/journald/tmux/sysfs — impossible from a container |
| CLI agent sessions | `tmux -L agents` | Survive client disconnects; `grave agents new/attach`; logs survive session death (`history`/`resume`, 📜/▶ in the dashboard) |
| Backing services | Docker compose stacks in `$GRAVE_ROOT/docker/` | Postgres, Redis, Playwright — disposable, loopback-bound |
| Control plane | `grave` (bash, `/usr/local/bin`) | One entrypoint for modes, doctor, logs, backup |
| Self-updater | `gravedecay-upgrade.service`, detached oneshot | Survives the dashboard restart caused by its own re-raise |

The macOS companion is separate and user-scoped: its fixed
`io.gravedecay.updater` LaunchAgent stages `$GRAVE_ROOT/repos/gravedecay` and
reruns `macos/install.sh`, never `raise.sh`; it preserves its two loopback
agents/Serve selection and does not include T3 or the Linux control plane.

## Isolated agent checkouts

On the Linux appliance, `grave agents new <name> --worktree --repo <repo>
[--branch <new-branch>]` creates a branch from the source checkout's committed
HEAD and starts a persistent shell in `$GRAVE_ROOT/worktrees/<repo>/<name>`.
`--repo` implies `--worktree`; the default branch is `agent/<name>`. The repo
must be a primary checkout at `$GRAVE_ROOT/repos/<repo>` with at least one
commit. Existing branches, session histories, and worktree paths are refused.
Uncommitted source-checkout changes are not copied. The existing
`grave agents new <name> [dir]` form still starts a shared-directory session.

Session metadata records the source repo, starting commit, initial branch,
and checkout path. The dashboard's live sessions and archive show that repo
and initial branch, with the path available on hover. This metadata follows
the existing owner/workspace authorization and is absent from the public
summary. A manually changed branch can differ from the recorded initial branch.

`grave agents kill <name>` stops exactly that session and retains its worktree.
`grave agents resume <name>` reuses the recorded worktree and metadata; a missing
or invalid worktree fails instead of falling back to the shared repository.
Lifecycle commands serialize through a per-root lock. They run as the invoking
Unix identity and introduce no root helper or shared collaborator checkout.
The macOS companion's separate CLI does not implement these commands.

`grave agents prune` is an explicit sweep. It retains live sessions, directories
used by other panes on the agent socket, modified/staged/untracked/ignored files,
and commits not merged into the source checkout's current HEAD or not reachable
from a locally known remote ref. No fetch is performed. An unchanged checkout
at its recorded starting commit is also eligible. Removal uses Git's non-forced
`worktree remove` (Git 2.17+); older Git retains the checkout with an explanation.
Branches and session history always remain. Pruned sessions cannot be resumed;
use a new session name. There is no automatic deletion on a timer or session kill.

`grave doctor` checks managed worktree storage is writable and each unpruned
session points to a worktree belonging to its recorded repository. Backups also
save worktree files and session metadata; see [RECOVERY.md](RECOVERY.md).

## Portable Docker work-plane

`docker/portable` runs T3, Claude/Codex CLIs, ttyd/tmux, and the dashboard as
an unprivileged application container behind an unprivileged nginx gateway.
Only nginx publishes one host-loopback port; its same-origin routes are `/`,
`/grave/`, and `/term/`. Project-scoped named volumes hold the workspace and a
separate agent HOME. The dashboard's `container` platform disables all host
observation and control server-side as well as in the UI.

The stack does not mount the Docker socket or use privileged, host networking,
host PID, systemd, or Tailscale. Host Tailscale Serve may proxy to its loopback
gateway. This makes it portable, but it cannot supply the native appliance's
firewall, doctor, backup, hardware, or gaming contracts.

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

Thin native clients consume the dashboard's loopback-backed, path-routed
`/grave/api/v1/summary` contract. It is a bounded local status sample, not a
second control plane: it excludes integrations, repositories, logs, and names.

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
worktrees/ isolated per-agent checkouts; branches share their source repo's Git objects
agents/    per-agent state: t3code server state, tmux session logs + meta.json
           (dir a session lived in — outlive the session for history/resume)
docker/    compose stacks (core, browsers, yours)
config/    grave.conf source-of-truth copies, tmux.conf, secrets/ (600, git-ignored)
scripts/   gravedecay.py, dashboard-static/ PWA shell assets, and helpers
logs/      grave.log, notifications.jsonl (🔔 inbox — durable copy of sent pages, 600)
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
No mode state files can drift. `grave gaming --for <timespan>` may add a named
transient systemd timer for the future developer transition; `grave status`
reports it and `grave developer` cancels it.

Automatic transitions are an independent gamewatch policy. The durable
`config/gamewatch.preference` (`on`/`off`) is synchronized to the watcher's
hot-reloaded flag. First raises default it on only for detected stock SteamOS
and off elsewhere. Detection is ordered and configurable (`gamescope`, active
Steam app cgroups, Feral GameMode, then an exact process name), so a profile can
tune signals without changing mode semantics.

## Always alive (tailnet keepalive)

The box is reached over the tailnet. A device that can't NAT-traverse to it (a
remote laptop on café/hotel/mobile Wi-Fi) falls back to a **DERP relay**. An
idle relayed path stalls, and the T3 / dashboard websocket reconnect-loops
(`Failed to connect. Reconnecting… disconnected`).

`gravedecay-keepalive.service` is an opt-in loop that pings every *online*
tailnet peer every `KEEPALIVE_SECONDS` (default 2, under the client's reconnect
gap) so the relay path never goes idle. It is installed on every box but idle
until the flag file `$GRAVE_ROOT/config/keepalive.on` exists — toggle it with
`grave keepalive on|off` or the dashboard ⚙️ **Always alive** row; the loop
re-reads the flag each poll. It reduces idle drops for relay-only clients but is
a mitigation, not a cure: a genuinely lossy uplink can still break the socket,
and a *direct* Tailscale connection (when NAT allows) is always better.

## The doctor contract

Every platform invariant is a `grave doctor` check. If a profile or a manual
tweak establishes something new ("GPU must be pinned", "docker on its own
subvolume"), it must add a check (via the `CHECK_*` flags or a profile edit) —
an invariant doctor can't see will silently regress.

The Linux self-updater operates on the checkout configured by `REPO_DIR`.
Tracked edits stop an upgrade and fail doctor because changing releases would
discard them. Non-conflicting untracked files, such as local field notes or a
saved installation transcript, are preserved across release and edge updates.
If an untracked path would be overwritten by the target release, Git stops the
upgrade and `grave` prints the conflicting path without changing that file.
Doctor also fails this readiness check if the checkout is missing or Git
cannot read its status; a failed status command is never treated as clean.

## Dashboard PWA boundary

The installed gravedecay web app owns the entire Tailscale Serve HTTPS origin,
not only `/grave/`. Its manifest starts at `/grave/` but declares scope `/` so
launcher navigation to T3 (`/`), the terminal (`/term/`), and pairing (`/pair/`)
stays in one standalone iOS, iPadOS, or macOS Safari app.

The terminal at `/term/` is ttyd serving a raise.sh-built custom frontend
(clipboard/OSC 52, touch-friendly copy — see TERMINAL.md); the dashboard adds
a per-session 📋 capture dialog as the copy path of last resort on devices
where in-terminal selection is impossible.

The dashboard is network-first because it controls a remote machine. API
responses, machine state, file listings, and action output are always
`no-store`. A service worker caches only a static connection-help page so a
disconnected launch explains how to restore Tailscale instead of showing a
blank browser error. The worker's cache name embeds a digest of that offline
page (stamped into `sw.js` at import, reported as `sw` on `/healthz`): browsers
only re-install a worker whose served bytes changed, so without the stamp an
upgrade touching `offline.html` would never reach already-installed PWAs.
`grave doctor` verifies the manifest scope, the root-scoped service worker
contract, and that the running stamp matches the installed offline page.

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
already-active unit. Dashboard health reports the running source hash and the on-disk PWA shell
hash (`index.html` under `scripts/dashboard-static/`). `grave doctor`
compares both with the installed files, making a stale process — or a
raise that updated the Python but not the shell — a visible contract
failure instead of silently serving an old UI.

### Issue-to-agent dispatch

The single-owner Linux dashboard's Linear widget can launch a saved issue
through `grave agents new --repo <repo> --task <private-json>`. The existing
lifecycle lock protects worktree/session creation. A private task runner
passes the fetched issue to Codex or Claude as a literal argument, records
process status, and retains the pane as a shell on exit. Deterministic session
names prevent duplicate dispatch, while Work links the issue, session, branch,
and any matching GitHub PR. See [DISPATCH.md](DISPATCH.md) for authorization,
platform scope, retry behavior, and recovery.

`gravedecay-agents.service` supervises persistent owner schedules through
`libexec/agent-jobs.py`. Each headless run gets a fresh Git worktree, private
transcript and atomic result record. The dashboard exposes bounded reports
only after owner authorization. See [SCHEDULES.md](SCHEDULES.md).
