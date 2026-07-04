# profiles/generic.sh — any always-on box with no special hardware quirks.
# Masks suspend/hibernate so the appliance never sleeps (MASK_SLEEP=0 to skip,
# e.g. if you want the box to still suspend on a schedule).
conf_set() { sudo sed -i "s|^$1=.*|$1=$2|" /etc/gravedecay/grave.conf; }

profile_apply() {
  if [[ "${MASK_SLEEP:-1}" == 1 ]]; then
    sudo systemctl mask sleep.target suspend.target hibernate.target hybrid-sleep.target
    conf_set CHECK_SLEEP_MASKED 1
  fi
}
