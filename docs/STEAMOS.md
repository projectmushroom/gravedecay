# Raising gravedecay on stock SteamOS

Stock SteamOS (Steam Machine / Steam Deck) is an **immutable, image-based OS**:
`/` is mounted read-only and every OS update *replaces the whole rootfs image*.
Anything installed into `/usr` with `pacman` — Docker, Node, ttyd, … — is wiped
on the next update. The `generic` ritual assumes a mutable, package-managed
distro, so it does not apply as-is.

gravedecay's answer is the **durable-hybrid** layout: keep everything the OS
update can't touch. An update leaves `/home` and `/var` alone (and preserves
user-added files in `/etc`), so that's where all of it goes.

| Piece | Where it lives | Survives update? |
|---|---|---|
| Repos, config, secrets, logs, docker volumes | `GRAVE_ROOT` under **`/home`** (not `/srv`) | ✅ |
| node, ttyd, jq, tailscale, compiler | **Homebrew** in `/home/linuxbrew` | ✅ |
| Docker engine | **rootless Docker** under `/home` (`~/bin`, `~/.local`) | ✅ |
| t3 (T3 Code) + its native `node-pty` | `~/.local`, compiled for the pinned Node LTS | ✅ |
| systemd units, `/etc/gravedecay`, sudoers | `/etc` (user-added files are preserved) | ✅ |
| tailscaled state | `/var/lib/tailscale` | ✅ |

Nothing gravedecay needs rides the rootfs image, so a SteamOS update just…
leaves it running. `raise.sh` auto-detects the immutable rootfs (via
`steamos-readonly status`) and switches to this layout on its own — it relocates
`GRAVE_ROOT` off `/srv`, skips `pacman`, uses the per-user Docker, and writes a
`tailscaled` unit for the Homebrew binary.

## 1. Bootstrap the durable toolchain (once)

You need a sudo password set (`passwd`) and network access. Then:

```sh
git clone https://github.com/projectmushroom/gravedecay
cd gravedecay
./steamos-toolchain.sh          # Homebrew + rootless Docker + t3, all under $HOME
```

`steamos-toolchain.sh` is idempotent and does the SteamOS-specific heavy
lifting:

- installs **Homebrew** to `/home/linuxbrew` and adds it to your `~/.bashrc`;
- `brew install`s `node@22`, `ttyd`, `jq`, `tailscale`, `slirp4netns`, and a
  compiler (`gcc`/`make`) plus `glibc` + `linux-headers`;
- installs **rootless Docker** (`get.docker.com/rootless`), enables it as a
  `--user` service, and turns on **linger** so it runs without a login session;
- builds **t3**'s native `node-pty` and installs t3 into `~/.local`.

### Why the compiler dance?

SteamOS ships **no C headers at all** (`/usr/include` is empty) and no compiler,
but t3 depends on `node-pty`, a native addon with no Linux prebuilt binary. The
bootstrap installs a Homebrew compiler and glibc/kernel headers, then wraps
`gcc`/`g++` to append those headers with **`-idirafter`** (so they're searched
*after* gcc's own headers — exactly where a system header's `#include_next
<stdlib.h>` looks). Homebrew's glibc is *older* than the system's, which is what
makes this safe: a shared `.node` compiled against the older headers resolves
its libc symbols against the running process's (newer) glibc at load time —
glibc is backward-compatible in that direction.

## 2. Raise the box

```sh
./raise.sh --profile steam-machine
grave doctor
```

The `steam-machine` profile records the immutable-rootfs invariants for
`grave doctor` (GRAVE_ROOT off the root mount, toolchain under `$HOME`, rootless
Docker), masks sleep/suspend (always-on), and relaxes the firewall check — see
the firewall note below.

The two interactive steps are unchanged: `sudo tailscale up --ssh` (then rerun
`raise.sh` so `tailscale serve` wires up the HTTPS origin), and T3 pairing.

## Firewall & gaming coexistence

A Steam Machine games, so the `steam-machine` profile does **not** impose a
host-wide default-deny firewall — that would break Steam Remote Play, local
multiplayer, and LAN discovery. The security boundary is instead:

- every gravedecay service binds `127.0.0.1` and is reachable only through
  `tailscale serve` (no LAN listener, no port forwarding);
- sshd is key-only.

The profile sets `CHECK_FIREWALL=0` so `grave doctor` reflects this. If you
don't use LAN gaming and want defense-in-depth, set it back to `1` and configure
firewalld (default zone `drop`, allow `ssh`, trust `tailscale0`).

## Updating

- **The base OS**: update SteamOS normally (Settings → System, or `steamos-update`).
  Because nothing gravedecay uses lives in the rootfs image, it keeps working
  across the update. If SteamOS ever resets a file under `/etc`, re-running
  `./raise.sh --profile steam-machine` restores it idempotently without touching
  your data.
- **The toolchain**: `grave update` runs `brew upgrade` on the immutable profile
  (it never calls `pacman`). t3 is pinned to its compiled Node LTS; rebuild it
  with `steamos-toolchain.sh` if you bump it.
- **gravedecay itself**: `grave upgrade` as usual.

## Reboot, auto-start & self-heal

SteamOS boots into Game Mode (gamescope), but that's just the graphical session —
the appliance comes up underneath it regardless:

- **System units** (`tailscaled`, `sshd`, `gravedecay`, `gravedecay-term`,
  `t3code`) are `enabled`, so they start at boot into `multi-user.target`. T3
  Code is a *system* unit running as your user, so it doesn't wait for a login.
- **Rootless Docker** is a *user* unit — normally it wouldn't start until you
  log in. The bootstrap enables **linger** (`loginctl enable-linger`), so your
  user systemd instance (and Docker) start at boot with no login. The stack
  containers carry `restart: unless-stopped`, so Docker brings them back too.
- `grave doctor` asserts all of this: every unit is `enabled` (not merely
  active), linger is on, and rootless Docker is enabled — "survives a reboot" is
  part of the contract, not an assumption.

So an unattended reboot lands you back at a fully-running box, reachable over the
tailnet, with Big Picture on the screen. Prove it once with a reboot test.

**Self-heal.** On an immutable host raise.sh also installs
`gravedecay-selfheal.service`, a boot-time oneshot that:

- verifies the `/etc` pieces grave depends on (config, units, sudoers, CLI)
  survived the last OS update — if any went missing it drops a marker
  `grave doctor` surfaces and logs the exact one-line fix
  (`cd <repo> && ./raise.sh --profile steam-machine`);
- otherwise makes sure the dev stacks actually came back (respecting
  `grave bootmode`).

It intentionally does **not** auto-run raise.sh headlessly: a full raise needs
broad passwordless sudo, which the platform deliberately doesn't keep (sudo is
scoped to specific commands). Flagging the rare `/etc`-reset with a clear
one-liner is the safer trade.

## Known rough edges (help wanted)

- Sensor names for the dashboard temps (`grave status` / System tab) aren't
  mapped for Steam Machine hardware yet.
- Controller-wake / HDMI-CEC behavior with sleep masked is untested.
- VRAM pressure when a heavy game and browser containers run at once.
