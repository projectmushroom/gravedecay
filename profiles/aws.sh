# profiles/aws.sh — an EC2 instance (tested: t3.medium, Amazon Linux 2023)
# raised as an always-on gravedecay appliance.
#
# Same shape as profiles/generic.sh (headless box, never sleeps) plus one
# thing worth calling out explicitly: a cloud instance has a real public IP
# attached by default, so the firewalld default-deny that raise.sh's dnf
# branch now sets up for AL2023 (see raise.sh, firewall step) is load-bearing
# here in a way it isn't on a box sitting behind a home router. CHECK_FIREWALL
# is set explicitly rather than relying on the config default so a future
# default change can't silently drop this invariant for cloud boxes.
#
# Two things this profile — and raise.sh in general — cannot do for you:
#
# 1. Tailscale Serve must be enabled for your tailnet once, in the admin
#    console, before `tailscale serve` will work: raise.sh prints the
#    enrollment URL if it hits "Serve is not enabled on your tailnet."
# 2. The EC2 security group is outside the box entirely. gravedecay's own
#    firewalld rules only gate traffic once it reaches the NIC — the security
#    group decides what reaches the NIC at all. Only allow inbound 22 (ssh)
#    from IPs/ranges you control; every gravedecay service (T3, dashboard,
#    web terminal) is reached over the tailnet, not a public port, so nothing
#    else needs to be open.
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
  if [[ "${MASK_SLEEP:-1}" == 1 ]]; then
    sudo systemctl mask sleep.target suspend.target hibernate.target hybrid-sleep.target
    conf_set CHECK_SLEEP_MASKED 1
  fi
  conf_set CHECK_FIREWALL 1
}
