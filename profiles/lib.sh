# profiles/lib.sh — helpers shared by host profiles. raise.sh sources
# profiles/<name>.sh, which sources this file relative to itself.
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
