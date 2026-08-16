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
Remote users are read-only: this companion does not configure allowed-user
write authorization. The dashboard intentionally has no T3 pairing/connect,
update/restart, terminal, Docker, gaming, reboot, journal, or Linux service
controls.

Run `macos/status.sh` for a read-only doctor-lite: it checks selected
LaunchAgents, loopback health, and Serve paths when applicable. `macos/uninstall.sh --root PATH` unloads and removes only its two LaunchAgents and Serve
path mounts. It preserves the Application Support data by default; `--purge`
explicitly removes it. Neither mode uninstalls Tailscale. DMG packaging and
notarization are future work.
