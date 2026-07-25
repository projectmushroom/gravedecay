# profiles/generic.sh — any always-on box with no special hardware quirks.
# Masks suspend/hibernate so the appliance never sleeps (MASK_SLEEP=0 to skip,
# e.g. if you want the box to still suspend on a schedule).
# Shared helpers (conf_set). Sourced relative to THIS file: raise.sh sources
# the profile by path, so BASH_SOURCE[0] is always the profile's own path.
# shellcheck disable=SC1091
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

profile_apply() {
  if [[ "${MASK_SLEEP:-1}" == 1 ]]; then
    sudo systemctl mask sleep.target suspend.target hibernate.target hybrid-sleep.target
    conf_set CHECK_SLEEP_MASKED 1
  fi
}
