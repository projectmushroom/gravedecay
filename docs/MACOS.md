# macOS companion (source install)

A Mac never takes the full raise; the companion is how it joins the
graveyard. It is a small, user-scoped install: the gravedecay dashboard on
`127.0.0.1:4712` and the network monitor on `127.0.0.1:4714`. It is not the
Linux appliance. It never installs or manages Docker, systemd, firewall
rules, SSH, or Tailscale; T3 and ttyd are also unmanaged unless you opt into
the agents layer (below). Tailscale is optional for localhost-only
use; install and sign into the official app when publishing tailnet paths.

If OrbStack or Colima is already installed (Docker Desktop also works) and its
standard `docker` CLI context works, the dashboard read-only lists its
containers. The companion never installs, starts, stops, or otherwise manages
Docker; an unavailable or unusable CLI simply shows `not managed on macOS`.
Publish any container ports to `127.0.0.1` only, then expose them deliberately
through Tailscale if needed.

Homebrew will be the normal install path when the planned
`projectmushroom/homebrew-gravedecay` tap publishes the release template in
this repository:

```sh
brew install projectmushroom/gravedecay/gravedecay-companion
gravedecay-mac install
```

`gravedecay-mac` is deliberately only a fixed wrapper for the existing
installer, status check, and uninstaller; it does not use `brew services`.
The companion retains ownership of its user LaunchAgents because it already
converges component choices, Serve mounts, identity gates, and unloads.

Contributors may instead run `macos/install.sh` from a source checkout. Both
paths use no `sudo`, install LaunchAgents and logs under
`~/Library/Application Support/Gravedecay`, and
normally publishes only `/grave` and `/net` through the already-installed
Tailscale CLI. Outside the opt-in agents layer it never changes the `/` Serve
mount, so an official T3 app or
T3 Connect remains outside its scope. Use `--dashboard-only`, `--network-only`,
`--agents` (T3 + web terminal, see below),
`--no-serve` (localhost only), `--allow-sleep` (skip the keep-awake agent),
`--root PATH` (an absolute descendant of your home directory), or `--dry-run`. Re-running is an
update; changing components converges by unloading the omitted agent and
removing its corresponding path mount.

Open `http://127.0.0.1:4712/` and `http://127.0.0.1:4714/` locally, or the
tailnet URLs `https://<mac>.ts.net/grave/` and `/net/` when Serve is enabled.
When enabling Serve, the installer reads `tailscale status --json` and binds
private dashboard data to the current Mac's exact Tailscale `LoginName`; it
fails closed if that identity cannot be determined. Other tailnet users see
only machine vitals, while localhost remains usable with `--no-serve`.

The **Work** tab scans nested Git repositories below `~/Sites` by default
(for example `~/Sites/owner/repo`). Change the absolute existing root in
**Settings → Work integrations**. It shows each local branch, dirty count and
last commit, and read-only open GitHub pull requests/issues plus latest CI for
repositories with a `github.com` origin. Discovery and API results are bounded
and briefly cached. Install and authenticate GitHub CLI (`gh auth login`) to
enable remote results; GitHub data is never changed. The same settings section
accepts an explicit Linear API key for assigned-to-me issues; the key is stored
locally with owner-only settings writes and is never returned to the browser.

Without the opt-in agents layer below, the dashboard intentionally has no T3
pairing/connect, update/restart, terminal, Docker management, gaming, reboot, journal,
or Linux service controls.

## The agents layer (opt-in): `--agents`

The observability-only default was written for a *personal* Mac, where any
remote action surface implicitly exposes the whole HOME and signed-in
identity. A Mac deliberately raised as a server (a mini in a drawer) can opt
into the core of what makes gravedecay valuable — T3 Code, persistent
`tmux -L agents` sessions, and the ttyd web terminal — none of which needs
Linux:

```sh
brew install projectmushroom/gravedecay/gravedecay-companion --with-node --with-tmux --with-ttyd
gravedecay-mac install --agents
```

The three optional formula dependencies install `tmux`, `ttyd`, and `node`.
A missing prerequisite is a preflight error, never a half-install; `t3` is
npm-installed if absent. Source-checkout users install those prerequisites
with `brew install tmux ttyd node` before `macos/install.sh --agents`.
Still no `sudo`, ever: two more user LaunchAgents run T3 Code on
`127.0.0.1:4711` (`io.gravedecay.t3`) and ttyd on `127.0.0.1:4713`
(`io.gravedecay.term`), and Serve adds `/` and `/term` so the origin layout
matches the appliance — the PWA, the native client, and pairing links work
unchanged. Pairing works exactly like Linux: ⚙️ settings → "🔑 New T3 pairing
token". The dashboard's sessions panel lists `tmux -L agents` sessions, ✕
kills them, and the launcher grows T3/Terminal/Claude/Codex tiles; every
reopened endpoint stays behind the exact-`LoginName` `ALLOWED_USERS` gate the
installer fails closed on. Agent sessions and T3's project root live in a
dedicated `~/Grave` directory, not the whole HOME — but terminal access is
still a shell as your Mac user (see [SECURITY.md](SECURITY.md)); for a
dedicated mini, prefer a separate macOS user account.

The Claude/Codex tiles (and the `gh` re-auth shortcut) expect those CLIs to
be installed separately — a tile whose command is missing just shows a dying
session; install the CLI and reopen it. The installer refuses to seize a `/`
Serve mount that something else already publishes.

`--agents` is a per-run opt-in, not sticky: re-running without it converges
back to observability-only (agents unloaded, `/` and `/term` mounts removed,
the endpoint allowlist restored). Live `tmux -L agents` sessions are never
killed implicitly — converging off prints how to attach or end them, and
neither uninstall nor `--purge` ever touches `~/Grave` (it is your work, not
companion data). The unattended updater preserves the mode.
Gaming/torpor, firewall management, Docker management, and multi-user workspaces remain
permanent non-goals on macOS.

## Native app publisher

The optional macOS 15+ native app is separate from this legacy source
companion. Its first run offers remote-first **Connect to Graves** or **Share
This Mac**; either can be enabled later. Settings → **Local host** → **Start
Local Host** starts a read-only, in-process listener only on `127.0.0.1:4712`
for `/healthz` and `/api/v1/summary`. It never installs a LaunchAgent, helper,
Python runtime, or Tailscale configuration. It refuses to start when this
legacy companion or another listener owns that port and reports `EXISTING
COMPANION ACTIVE`.

When the native app cannot find Tailscale, Graveyard, Settings, and the menu
bar offer **Get Tailscale** or **Open Tailscale**. They only open the official
download page or app: sign-in and Tailscale changes remain yours.

It does not modify Tailscale Serve: the UI shows the exact manual command to
publish `/grave` after the listener is healthy. A successfully started native
host is restored once when the app launches; enable Launch at Login to restore
that opted-in host after sign-in. If the legacy companion later owns the port,
the app retains the request but fails closed until the conflict is resolved.

The System view uses only native, unprivileged macOS data: CPU activity from
`top`, reclaimability from `memory_pressure` (labelled **Memory pressure**, not
RAM used), installed and compressed-memory detail from `sysctl`/`vm_stat`, disk
usage, `pmset` thermal limits and battery state, and swap use from `sysctl`.
The dashboard briefly caches native sampling so a normal refresh does not run
`top` more often than about every five seconds. Exact CPU/GPU temperatures and
fan RPM are intentionally not shown: macOS exposes those SMC readings through
privileged `powermetrics` or third-party SMC tooling, neither of which this
user-scoped, no-sudo companion installs or depends on. Thermal is instead
reported as nominal or throttled from `pmset -g therm`; desktop Macs simply
have no battery card.

Run `macos/status.sh` for doctor-lite. Like `grave doctor` on the appliance,
it is the contract: it exits non-zero when an invariant fails. It checks
selected LaunchAgents loaded, loopback health, Serve path mounts matching the
enabled components, installed checkout/version/channel, the detached updater
and periodic doctor agents, the keep-awake agent and sleep assertion while
serving, install drift (`gravedecay.py`/`gravenet.py` are copies — a managed
checkout that moved on without a rerun fails the check), and ntfy secret
hygiene/reachability when configured. The installed copy runs every 30
minutes as `io.gravedecay.doctor`; with an ntfy topic configured a failing
run pages you (`--page`, see [NOTIFICATIONS.md](NOTIFICATIONS.md)).

`gravedecay-mac uninstall` (or `macos/uninstall.sh --root PATH` from a source
checkout) unloads and removes its LaunchAgents
(including the updater, keep-awake and doctor agents) and Serve path mounts.
It preserves the Application Support data by default; `--purge`
explicitly removes it. For a Homebrew removal, run the companion command
first, then `brew uninstall projectmushroom/gravedecay/gravedecay-companion`;
use `gravedecay-mac uninstall --purge` before that only when you explicitly
want to delete Application Support data. Neither mode uninstalls Tailscale. Native-app DMG,
signing, and notarization details live in [clients/apple/README.md](../clients/apple/README.md).

## Staying awake while serving

A Mac that publishes tailnet paths but idle-sleeps is worse than no server —
the paths just go dark. When Serve is enabled the installer therefore loads
`io.gravedecay.keepawake`, which holds a `caffeinate -si` assertion for as
long as launchd runs; pass `--allow-sleep` (persisted across reruns) if you
are on a laptop and *want* sleep. The no-sudo ceiling, honestly: caffeinate
prevents idle sleep, but closing the lid still sleeps a MacBook unless it is
on AC power in clamshell mode (external display + input device). Overriding
lid-close sleep is `sudo pmset disablesleep 1` territory, which this
user-scoped installer refuses — run it yourself if you accept the
lid-thermals caveat. `status.sh` fails when serving is enabled, sleep is not
opted out, and the agent or its assertion is missing.

## Updating the companion

After the tap is published, update the formula then re-run its command:

```sh
brew upgrade projectmushroom/gravedecay/gravedecay-companion
gravedecay-mac install
```

The wrapper passes the formula's exact release tag to the installer. On the
next run it stages that tag into the existing managed checkout before the
normal converge, so an upgraded Cellar copy cannot be bypassed. Re-run with
`--agents` when that mode is enabled. Formula releases are rendered from
[`macos/homebrew/gravedecay-companion.rb.tmpl`](../macos/homebrew/gravedecay-companion.rb.tmpl)
in the tap; this repository intentionally does not create or publish the tap.

The first manual rerun from an older companion (including v0.17.0) bootstraps
an installer-controlled checkout at `$GRAVE_ROOT/repos/gravedecay`, installs
the user updater and records `$GRAVE_ROOT/config/release.json` (exact release
or development checkout plus channel). Use `grave releases --json`, `grave
upgrade --release`, `grave upgrade --tag v0.20.0`, or `grave upgrade --edge`.
The dashboard provides the configured-channel action and exact picker through
its fixed installed helper, never interactive `PATH`.

`io.gravedecay.updater` stages a clean trusted checkout, preserves component
and Serve choice, and restarts only dashboard/network agents. Failed fetch,
install, or health work restores the prior payload. Status is bounded in
`config/update-status.json`, logs stay under `$GRAVE_ROOT/logs`, and no new
port is added. Normal uninstall keeps source/data; `--purge` is required to
remove them. T3, Tailscale connection, Docker, SSH and Linux services remain
unmanaged.

The installer creates `~/.local/bin/grave` only when that path is absent or
already points to this companion; add `~/.local/bin` to your shell `PATH` if it
is not already present. It never edits shell startup files or replaces another
`grave` command. The updater keeps the installer-recorded Serve owner identity
instead of querying or logging it again, and removes only its own hook on
uninstall.
