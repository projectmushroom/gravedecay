#!/usr/bin/env bash
# tests/e2e/bootstrap.sh — runs as root INSIDE the smoke container, before the
# first raise. Shapes the box like a real appliance host: an unprivileged
# owner in a password-requiring wheel group (the SteamOS shape that broke
# #89/#96 — with no password set, any out-of-scope sudo fails exactly like a
# headless box), stubs for the two externals CI can't have (tailscale needs a
# tailnet, t3 needs provider auth), and netfilter-dependent ufw swapped for a
# deterministic stand-in.
set -euo pipefail

# The appliance owner. The zzz- bootstrap grant stands in for the human at
# the first-raise keyboard (sudo is last-match-wins, so it must sort AFTER
# the wheel file to beat it); smoke.sh deletes it before the headless
# re-raise, leaving only wheel (password) + the raise-installed scoped grant.
useradd -m -G wheel mole 2>/dev/null || true
printf '%%wheel ALL=(ALL) ALL\n' >/etc/sudoers.d/wheel
printf 'mole ALL=(ALL) NOPASSWD: ALL\n' >/etc/sudoers.d/zzz-e2e-bootstrap
chmod 440 /etc/sudoers.d/wheel /etc/sudoers.d/zzz-e2e-bootstrap

# sshd: key-only and running — doctor asserts both.
ssh-keygen -A
mkdir -p /etc/ssh/sshd_config.d
printf 'PasswordAuthentication no\n' >/etc/ssh/sshd_config.d/50-e2e.conf
systemctl enable --now sshd

# tailscale stub + a tailscaled stand-in owning the LocalAPI socket that
# raise's permission guards stat (group/mode must match the appliance owner).
install -m 755 /repo/tests/e2e/fake-tailscale /usr/local/bin/tailscale
# Type=notify mirrors real tailscaled: systemd holds the unit "activating"
# until the daemon signals readiness (after the LocalAPI socket exists), so the
# gravedecay-localapi ExecStartPost chgrp/chmod can't race socket creation. The
# stub creates the socket, then signals ready via systemd-notify (NotifyAccess=all
# because the notifier is a child of the ExecStart shell, not the main PID).
# Keeps the stub faithful to the shipped unit and satisfies doctor's Type=notify.
cat >/etc/systemd/system/tailscaled.service <<'EOF'
[Unit]
Description=tailscaled stand-in for the appliance smoke
[Service]
Type=notify
NotifyAccess=all
ExecStart=/bin/sh -c 'mkdir -p /run/tailscale && : > /run/tailscale/tailscaled.sock && chgrp mole /run/tailscale/tailscaled.sock && chmod 660 /run/tailscale/tailscaled.sock && systemd-notify --ready && exec sleep infinity'
[Install]
WantedBy=multi-user.target
EOF
systemctl enable --now tailscaled

# t3 stub (raise skips its npm install when `command -v t3` succeeds).
install -m 755 /repo/tests/e2e/fake-t3 /usr/local/bin/t3

# ufw in a CI container depends on host netfilter modules — flaky. Overwrite
# the PACKAGED binary with a stand-in that logs calls and reports the
# default-deny state doctor asserts. The pacman package stays installed (so
# raise's package check is satisfied) and the path stays /usr/bin/ufw — the
# exact path the scoped sudoers grants, keeping the headless-sudo contract
# under test.
cat >/usr/bin/ufw <<'EOF'
#!/usr/bin/env bash
echo "ufw-e2e: $*" >>/var/log/ufw-e2e.log
if [[ "${1:-}" == status ]]; then
  printf 'Status: active\nDefault: deny (incoming), allow (outgoing)\n'
fi
exit 0
EOF
chmod 755 /usr/bin/ufw

# Real docker-in-container: the core stack (postgres+redis) must actually
# come up. The playwright browsers image is ~2 GiB, so pre-seed that stack
# with a tiny stand-in — raise deliberately never overwrites an existing one.
systemctl enable --now docker
install -d -o mole -g mole /srv/dev /srv/dev/docker /srv/dev/docker/browsers
cat >/srv/dev/docker/browsers/compose.yaml <<'EOF'
services:
  browsers:
    image: alpine:3
    command: sleep infinity
    init: true
    restart: unless-stopped
    networks: [devnet]
networks:
  devnet:
    external: true
EOF
chown -R mole:mole /srv/dev
echo "bootstrap complete"
