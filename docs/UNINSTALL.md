# Uninstalling — unraising the box

`grave uninstall` is the inverse of `raise.sh`. It removes the appliance and
leaves the machine behind: still on the tailnet, still with Docker and its
toolchain, still holding your repositories.

```sh
grave uninstall --dry-run   # print the whole teardown, change nothing
grave uninstall             # remove the platform, keep the data
grave uninstall --purge     # also delete $GRAVE_ROOT and every docker volume
```

Both forms prompt for confirmation (`--yes` skips it, for scripted teardowns).
`--purge` asks you to type `purge <hostname>` — a phrase you cannot produce by
holding down Enter.

## The two rules

**1. Remove what gravedecay installed; leave what it merely used.**
Docker, Homebrew, Node, `ttyd`, `t3` and your distro packages were either here
before the appliance or are useful without it, and other things on the box may
depend on them. They stay.

Tailscale is the sharpest case. Uninstall removes the `tailscale serve` mounts
gravedecay created (`/`, `/grave`, `/term`, `/net`, plus any `grave preview`
tunnels) and the LocalAPI drop-in, but it **never logs the node out**. On a
remote box — a Steam Machine in the living room, an EC2 instance — that link is
how you are watching the uninstall happen. Leaving the tailnet is a separate,
deliberate act:

```sh
sudo tailscale logout
```

**2. Data is not platform.**
`$GRAVE_ROOT` holds your repositories, agent session history, secrets and
backups. A default uninstall does not touch it, and Docker *volumes* (your
databases) survive too — only the containers are removed. A wrong uninstall
should cost you a re-raise, not your work.

Re-raising onto a kept `$GRAVE_ROOT` is an adoption, not a fresh install:

```sh
$GRAVE_ROOT/repos/gravedecay/raise.sh --profile <your-profile>
```

## What is removed

| | Default | `--purge` |
|---|---|---|
| systemd units (dashboard, t3code, term, net, gateway, timers, watchers) | removed | removed |
| `grave` + `grave-workspaces` CLIs | removed | removed |
| `/etc/gravedecay`, sudoers drop-in, `/usr/libexec/gravedecay` | removed | removed |
| gravedecay `tailscale serve` mounts + previews | removed | removed |
| T3 Connect link (if a mode was declared) | unlinked | unlinked |
| `grave-agent-notify` hooks in `~/.claude` / `~/.codex` | removed | removed |
| sleep/suspend masks (if the profile set them) | unmasked | unmasked |
| core + browsers containers | removed | removed |
| `devnet` docker network | removed | removed |
| **docker volumes (databases)** | **kept** | deleted |
| **`$GRAVE_ROOT`** (repos, agents, secrets, backups) | **kept** | deleted |
| `~/Projects` symlink | kept (still valid) | removed |
| docker, tailscale login, toolchain, distro packages | kept | kept |
| workspace unix users (`grave-<slug>`) | kept | kept |

### Things it deliberately will not do

- **Log out of Tailscale** — see rule 1.
- **Delete workspace unix users.** In multi-user mode each `grave-<slug>` home
  holds that collaborator's own credentials (GitHub, Linear, agent logins).
  Deleting them is a decision about someone else's data, so uninstall reports
  the accounts and leaves them. Remove them yourself when you mean to:
  `sudo userdel -r grave-<slug>`.
- **Remove snapper configs or snapshots.** If `CHECK_SNAPPER=1` was set, the
  timeline config and its snapshots are reported and left in place.
- **Uninstall packages.** `pacman`/`apt`/`dnf`/Homebrew are untouched.

Everything in this list is printed under **"Left on this box (by design)"** at
the end of the run, so what remains is never a guess.

## Why the sleep masks come off

`profiles/steam-machine.sh` masks `sleep.target`, `suspend.target`,
`hibernate.target` and `hybrid-sleep.target` so the appliance is always
reachable. That is gravedecay policy, not a pre-existing system setting — left
behind it would silently keep a Steam Machine from ever suspending again after
the appliance is gone. Uninstall unmasks them when `CHECK_SLEEP_MASKED=1`.

## T3 Connect

If `grave t3 connect` declared `publish` or `full`, uninstall runs
`t3 connect unlink` first. A live relay link that outlives the box leaves the
official T3 apps pointed at something that no longer answers, and keeps holding
the account's managed-tunnel slot — see "Freeing a managed-tunnel slot" in
[SECURITY.md](SECURITY.md).

If the `t3` CLI is already gone but a mode is still declared, uninstall says so
loudly rather than skipping it: you will need to unlink from another
environment.

## When the CLI is broken

`grave` refuses to start without `/etc/gravedecay/grave.conf`, so a box whose
`/etc` was wiped (a SteamOS update, a bad restore) cannot uninstall itself the
normal way. Use the repo script:

```sh
$GRAVE_ROOT/repos/gravedecay/uninstall.sh          # or from any checkout
```

It prefers to hand off to the installed CLI. When it cannot — no `grave`
binary, or no readable config — it falls back to removing the fixed surface it
can identify without configuration: units, `/etc` entries, sudoers drop-ins, the
CLIs, and any masked sleep targets. Anything config-dependent (containers,
`$GRAVE_ROOT`, tailnet mounts) is printed as a short list of commands for you to
run. `--purge` is **not** honored on the fallback path — it will not guess at
which directory holds your data.

## Verifying

After a default uninstall:

```sh
systemctl list-units 'gravedecay*' 't3code*'   # nothing
command -v grave                               # nothing
ls /etc/gravedecay                             # No such file or directory
tailscale serve status                         # no gravedecay mounts
curl -sf http://127.0.0.1:4711/                # refused (see docs/PORTS.md)
```

`grave doctor` is gone with the rest of the platform — it is the contract for a
*raised* box, and there is nothing left for it to check.
