# profiles/generic.sh — any always-on box with no special hardware quirks.
# Masks suspend/hibernate so the appliance never sleeps (MASK_SLEEP=0 to skip,
# e.g. if you want the box to still suspend on a schedule).
conf_set() {
  # Rewrite the key if present, else append it. A plain `sed s|^K=.*|` silently
  # no-ops when the key is missing (an older grave.conf preserved across upgrade),
  # so a CHECK_* invariant the profile sets would never reach `grave doctor`.
  if sudo grep -q "^$1=" /etc/gravedecay/grave.conf; then
    sudo sed -i "s|^$1=.*|$1=$2|" /etc/gravedecay/grave.conf
  else
    printf '%s=%s\n' "$1" "$2" | sudo tee -a /etc/gravedecay/grave.conf >/dev/null
  fi
}

profile_apply() {
  if [[ "${MASK_SLEEP:-1}" == 1 ]]; then
    sudo systemctl mask sleep.target suspend.target hibernate.target hybrid-sleep.target
    conf_set CHECK_SLEEP_MASKED 1
  fi
}
