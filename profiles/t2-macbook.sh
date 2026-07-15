# profiles/t2-macbook.sh — Intel MacBooks with the Apple T2 chip running Linux
# (tested: MacBookPro16,1 / CachyOS). Two hardware realities:
#
# 1. The AMD dGPU's SMU hangs on s2idle suspend, panel power-off, and AC↔DC
#    transitions — so the box must never sleep and the lid must be ignorable.
# 2. amdgpu runtime power management triggers the same hang — pinning the GPU
#    to a fixed DPM performance level keeps it stable. The pin must run as a
#    systemd service ordered after amdgpu init (a udev rule races it and
#    silently never applies).
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
  # never sleep
  sudo systemctl mask sleep.target suspend.target hibernate.target hybrid-sleep.target
  conf_set CHECK_SLEEP_MASKED 1

  # ignore the lid at logind level (box lives closed in a corner). Compare
  # first: logind.conf.d is outside the scoped sudoers, so a steady-state
  # re-raise (#89) must not rewrite it — and skipping also spares an
  # unnecessary logind restart on every raise.
  lid_conf=/etc/systemd/logind.conf.d/50-gravedecay-lid.conf
  lid_want=$'[Login]\nHandleLidSwitch=ignore\nHandleLidSwitchExternalPower=ignore\nHandleLidSwitchDocked=ignore'
  if [[ "$(cat "$lid_conf" 2>/dev/null)" != "$lid_want" ]]; then
    [[ -d /etc/systemd/logind.conf.d ]] || sudo mkdir -p /etc/systemd/logind.conf.d
    printf '%s\n' "$lid_want" | sudo tee "$lid_conf" >/dev/null
    sudo systemctl restart systemd-logind
  fi
  conf_set CHECK_LID_IGNORED 1

  # pin amdgpu to a fixed DPM state (level 3 ≈ mid clock; adjust per card)
  sudo tee /etc/systemd/system/amdgpu-pstate-pin.service >/dev/null <<'EOF'
[Unit]
Description=Pin amdgpu to a fixed DPM performance level (T2 MacBook SMU hang workaround)
After=systemd-udev-settle.service graphical.target
Wants=systemd-udev-settle.service

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/bin/sh -c 'for d in /sys/class/drm/card*/device; do \
  [ -f "$d/power_dpm_force_performance_level" ] || continue; \
  echo manual > "$d/power_dpm_force_performance_level"; \
  echo 3 > "$d/pp_dpm_sclk" 2>/dev/null || true; done'

[Install]
WantedBy=multi-user.target
EOF
  sudo systemctl daemon-reload
  sudo systemctl enable --now amdgpu-pstate-pin

  # doctor should watch the pin service too
  # append only once — the sed matches its own output, so an unguarded rerun
  # keeps growing ALWAYS_ON=(… amdgpu-pstate-pin amdgpu-pstate-pin …).
  grep -q 'amdgpu-pstate-pin' /etc/gravedecay/grave.conf \
    || sudo sed -i 's|^ALWAYS_ON=(\(.*\))|ALWAYS_ON=(\1 amdgpu-pstate-pin)|' /etc/gravedecay/grave.conf
}
