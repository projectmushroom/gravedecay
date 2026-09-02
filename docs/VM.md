# Raising in a plain VM

A VM from a hypervisor (KVM, Proxmox, VMware, a friend's blade) is just a
headless Linux box: `--profile generic` is the right profile and nothing VM
specific is needed. The hardware bits raise.sh ships (sensors, gamewatch,
sleep masking) already go quiet when the hardware is absent.

## Pre-flight (30 seconds)

Run these before `./raise.sh`. If any fails, the "VM" is a container and
gravedecay is the wrong tool for it.

```sh
systemctl is-system-running        # running or degraded — anything else: no systemd PID 1
ls -l /dev/net/tun                 # tailscale needs a TUN device
sudo docker info >/dev/null && echo docker ok   # after install: no nesting restriction
```

Ask the host for a real disk (not a thin overlay) and ~8 GB RAM. Snapshots
are the hypervisor's job here; skip the btrfs/snapper steps.

## openSUSE Leap 15.6

Handled by the zypper branch in raise.sh. What it works around:

- `/usr/bin/python3` is 3.6; `grave` needs 3.8+. raise.sh installs `python311`
  and points the units at the newest `python3.x` on PATH.
- Plain `nodejs` is too old for T3 Code; `nodejs22` + `npm22` are installed.
- `ttyd` is not in the Leap repos; the pinned upstream binary is fetched
  (same path as Amazon Linux).
- firewalld is the default. The generic profile leaves it alone. Harden by
  hand: default zone `drop`, allow `ssh`, trust `tailscale0`. Or use
  `--profile aws`, whose wrapper does exactly that and verifies the ssh rule
  before reload.

## Verify

`grave doctor`, then the checks in `CLAUDE.md` step 3.
