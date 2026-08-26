# macOS companion (source install)

The macOS path is a small, user-scoped companion: the gravedecay dashboard on
`127.0.0.1:4712` and the network monitor on `127.0.0.1:4714`. It is not the
Linux appliance. It never installs or manages T3, Docker, ttyd, systemd,
firewall rules, SSH, or Tailscale. Tailscale is optional for localhost-only
use; install and sign into the official app when publishing tailnet paths.

From a source checkout, run `macos/install.sh`. It uses no `sudo`, installs
LaunchAgents and logs under `~/Library/Application Support/Gravedecay`, and
normally publishes only `/grave` and `/net` through the already-installed
Tailscale CLI. It never changes the `/` Serve mount, so an official T3 app or
T3 Connect remains outside its scope. Use `--dashboard-only`, `--network-only`,
`--no-serve` (localhost only), `--root PATH` (an absolute descendant of your
home directory), or `--dry-run`. Re-running is an
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

The dashboard intentionally has no T3 pairing/connect, update/restart,
terminal, Docker, gaming, reboot, journal, or Linux service controls.

## Native app publisher

The optional macOS 15+ native app is separate from this legacy source
companion. Its Settings → **Advertise This Mac** starts a read-only,
in-process listener only on `127.0.0.1:4712` for `/healthz` and
`/api/v1/summary`. It never installs a LaunchAgent, helper, Python runtime,
or Tailscale configuration. It refuses to start when this legacy companion or
another listener owns that port and reports `EXISTING COMPANION ACTIVE`.

It does not modify Tailscale Serve: the UI shows the exact manual command to
publish `/grave` after the listener is healthy. The listener stops with the
app; enable Launch at Login if the app should be available after sign-in.

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

Run `macos/status.sh` for a read-only doctor-lite: it checks selected
LaunchAgents, loopback health, Serve paths, installed checkout/version/channel, and the detached updater. `macos/uninstall.sh --root PATH` unloads and removes its LaunchAgents (including the updater) and Serve
path mounts. It preserves the Application Support data by default; `--purge`
explicitly removes it. Neither mode uninstalls Tailscale. DMG packaging and
notarization are future work.

## Updating the companion

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
