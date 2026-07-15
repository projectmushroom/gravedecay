# tests/e2e/Dockerfile — a disposable Arch box with systemd as PID 1, used by
# smoke.sh to raise a REAL appliance in CI (#85). Arch because it is the
# first-class distro family. The full raise.sh package list is pre-baked so
# the image build absorbs the network cost and the raise's --needed install
# is a no-op on a warm box.
FROM archlinux:base
RUN pacman -Syu --noconfirm archlinux-keyring && \
    pacman -Syu --noconfirm systemd sudo git tmux curl jq python python-pillow \
      python-cryptography nodejs npm ttyd lm_sensors ufw docker docker-compose \
      openssh && \
    pacman -Scc --noconfirm
RUN systemd-machine-id-setup && systemctl set-default multi-user.target
STOPSIGNAL SIGRTMIN+3
CMD ["/usr/lib/systemd/systemd"]
