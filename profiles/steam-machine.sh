# profiles/steam-machine.sh — Valve Steam Machine / Steam Deck running stock
# SteamOS as a 24/7 gravedecay appliance that still games.
#
# SteamOS is an IMMUTABLE, image-based OS: `/` is read-only and every OS update
# replaces it, so nothing we install into /usr survives. gravedecay handles
# this by keeping the whole toolchain under $HOME (Homebrew + rootless Docker)
# and moving GRAVE_ROOT off /srv (which rides the root image) onto $HOME.
# raise.sh auto-detects the immutable rootfs; this profile records the
# resulting invariants for `grave doctor` and applies the always-on tweaks.
#
# BEFORE raising, bootstrap the durable toolchain once — see docs/STEAMOS.md.
# (raise.sh will stop with a clear message if a required tool is missing.)
#
# Model: SteamOS boots into gamescope/Game Mode as usual; gravedecay's services
# are always-on and reachable over the tailnet the whole time. `grave gaming`
# still frees RAM/GPU by freezing agents + stopping dev services when you want
# maximum headroom, but it isn't required — the appliance coexists with play.
#
# STATUS: stock-SteamOS support. Refine on real hardware (sensor names for the
# dashboard, controller-wake behavior, HDMI-CEC, VRAM pressure in game mode).
conf_set() {
  # Rewrite the key if present, else append it. A plain `sed s|^K=.*|` silently
  # no-ops when the key is missing (an older grave.conf preserved across upgrade),
  # so a CHECK_* invariant the profile sets would never reach `grave doctor`.
  # Exact value already present -> skip entirely: keeps a steady-state re-raise
  # sudo-free (#89); grave.conf is world-readable so neither check needs sudo.
  grep -qxF "$1=$2" /etc/gravedecay/grave.conf 2>/dev/null && return 0
  if grep -q "^$1=" /etc/gravedecay/grave.conf; then
    sudo sed -i "s|^$1=.*|$1=$2|" /etc/gravedecay/grave.conf
  else
    printf '%s=%s\n' "$1" "$2" | sudo tee -a /etc/gravedecay/grave.conf >/dev/null
  fi
}

profile_apply() {
  # the appliance never sleeps; Steam's own suspend UI is bypassed by this
  sudo systemctl mask sleep.target suspend.target hibernate.target hybrid-sleep.target
  conf_set CHECK_SLEEP_MASKED 1

  # record the immutable-rootfs invariants so doctor enforces them: GRAVE_ROOT
  # off the root mount, durable toolchain under $HOME, rootless Docker.
  conf_set CHECK_IMMUTABLE_ROOT 1

  # A Steam Machine games: leave the LAN open for Steam Remote Play, local
  # multiplayer, and discovery instead of a host-wide default-deny. The security
  # boundary is that every gravedecay service binds 127.0.0.1 and is reachable
  # only via `tailscale serve`, plus key-only sshd. Relax doctor's firewall check
  # to match. If you don't use LAN gaming, set this back to 1 and configure
  # firewalld (default zone drop, allow ssh, trust tailscale0).
  conf_set CHECK_FIREWALL 0

  # keep the box reachable when a game session is hogging the GPU: nothing to
  # do — ALWAYS_ON services (tailscaled, sshd, gravedecay) are never touched by
  # gaming mode. Verify after first game night with: grave doctor

  # Game-mode auto-throttle ON by default on a Steam Machine: launching a game
  # freezes agents + frees RAM/GPU, exiting restores them. raise.sh installed
  # the watcher; this flips its flag on. Disable any time with `grave gamewatch
  # off`.
  mkdir -p "$GRAVE_ROOT/config"
  : > "$GRAVE_ROOT/config/gamewatch.on"
}
