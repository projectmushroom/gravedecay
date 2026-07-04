# profiles/steam-machine.sh — Valve Steam Machine (2026) as a 24/7 gravedecay
# appliance that still games.
#
# Assumes the box runs an Arch-family desktop distro (e.g. CachyOS with
# gamescope-session) rather than stock SteamOS: stock SteamOS has an immutable
# rootfs and needs a different approach (report to the human before hacking
# around it).
#
# Model: desktop-first boot; gaming is entered via the gamescope session or
# `grave gaming`, which frees RAM/GPU by stopping dev services. Suspend is
# masked — a Steam Machine drawing a few watts idle IS the appliance.
#
# STATUS: initial cut — refine on real hardware (fan/temp sensor names for
# gravedecay, controller-wake behavior, HDMI-CEC, VRAM pressure in game mode).
conf_set() { sudo sed -i "s|^$1=.*|$1=$2|" /etc/gravedecay/grave.conf; }

profile_apply() {
  # the appliance never sleeps; Steam's own suspend UI is bypassed by this
  sudo systemctl mask sleep.target suspend.target hibernate.target hybrid-sleep.target
  conf_set CHECK_SLEEP_MASKED 1

  # keep the box reachable when a game session is hogging the GPU: nothing to
  # do — ALWAYS_ON services (tailscaled, sshd, gravedecay) are never touched by
  # gaming mode. Verify after first game night with: grave doctor
}
