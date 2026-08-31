# profiles/aws.sh — an EC2 instance (tested: t3.medium, Amazon Linux 2023)
# raised as an always-on gravedecay appliance.
#
# Same shape as profiles/generic.sh (headless box, never sleeps) plus one
# thing worth calling out explicitly: a cloud instance has a real public IP
# attached by default, so the firewalld default-deny raise.sh applies for
# this profile (via /usr/libexec/gravedecay/firewall-harden — see raise.sh,
# firewall step; other profiles are deliberately left untouched) is
# load-bearing here in a way it isn't on a box sitting behind a home router.
# CHECK_FIREWALL is set explicitly rather than relying on the config default
# so a future default change can't silently drop this invariant for cloud
# boxes.
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
# Shared helpers (conf_set). Sourced relative to THIS file: raise.sh sources
# the profile by path, so BASH_SOURCE[0] is always the profile's own path.
# shellcheck disable=SC1091
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

profile_apply() {
  sudo systemctl mask sleep.target suspend.target hibernate.target hybrid-sleep.target
  conf_set CHECK_SLEEP_MASKED 1
  conf_set CHECK_FIREWALL 1
}
