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

## Legacy openSUSE Leap 42.3 (x86_64)

The Leap 15.6 package recipe is not sufficient for 42.3. Its glibc 2.22,
Python 3.4, Git 2.13, systemd 228 and Docker 18.09 need additional handling.
The opt-in bootstrap below reproduces the runtime used on a Leap 42.3 KVM VM;
it is not a general installer for old distributions.

As the appliance owner, with sudo available:

```sh
# First install the distro daemon and build/download prerequisites.
sudo zypper install docker gcc glibc-devel curl tar xz diffutils
./compat/leap42/install.sh --root /srv/dev
./raise.sh --profile generic --root /srv/dev
./compat/leap42/install.sh --check
grave doctor
```

The compatibility installer pins Node 22.22.2's experimental
[glibc-2.17 build](https://github.com/nodejs/unofficial-builds), uv 0.12.9 when
uv is absent, Python 3.11.16 in an owner-local environment, compatible Python
wheels, Docker CLI 27.5.1, and Compose 2.30.3. Binary downloads have SHA-256
pins in the script. T3 0.0.38 is installed only when the dedicated Node prefix
has no T3 installation. Existing T3 installations are preserved. These are
tested compatibility pins, not promises of future version compatibility.

The `memfd.c` source builds the small glibc wrapper required by T3's native
terminal dependency. Only the `/usr/local/bin/t3` launcher preloads it; the
system glibc is not replaced. Node and npm wrappers select the dedicated
`/opt` prefix, and Python wrappers select the owner-local environment. The
Docker client is installed separately from the distro daemon. The installer
does not restart services or containers. Reapply it after replacing those
wrappers or `/usr/local`; use `--check` for read-only verification.

The marker `config/leap42-compat` tells raise to keep the custom runtime and
adds a doctor check for Python dependencies, Node, the actual memfd syscall,
T3, and Compose. Runtime downloads are not repeated during ordinary raise.
Service templates use the Unix account's primary group, which on openSUSE
can be `users` rather than a group named after the account. No supplemental
same-named group is needed.

### Explicit Docker 18.09 exception

If PostgreSQL or Playwright fails because Docker 18.09's seccomp profile
rejects newer syscalls, rerun the compatibility installer with
`--legacy-seccomp`, then rerun raise. This installs small Compose override
files for **only PostgreSQL and Playwright**, keeps their loopback port binds,
and refuses to overwrite a different operator override. The flag is rejected
on other daemon versions. Redis retains its normal seccomp policy.

This removes syscall filtering for those two containers; Docker
[documents the effect of `seccomp=unconfined`](https://docs.docker.com/engine/security/seccomp/).
It is never enabled by default. Prefer upgrading the daemon when possible;
after that, remove the two added override entries and recreate those services.
Existing hand-edited compose files must have their old exceptions removed too.

### Older command-line tools

Package detection prefers zypper over openSUSE's unrelated apt-get frontend.
Release listing uses `git symbolic-ref`; doctor reads `systemctl show`
without the newer `--value` option. Timed game mode omits `--collect` where
unsupported. On systemd without `systemd-analyze timespan`, accepted durations
are concatenated numbers with `ms`, `s`, `min`, `m`, `h`, `d`, or `w` units
(for example `90m` or `1h30m`); the manager validates the resulting timer.

Validation on this VM covers runtime checks and focused regression tests.
A fresh installation on an empty Leap 42.3 VM still needs independent testing;
this recipe does not upgrade the operating system itself.
