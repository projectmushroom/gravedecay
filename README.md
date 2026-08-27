<p align="center">
  <img src="assets/gravedecay-skull.svg" width="128" alt="gravedecay skull logo">
</p>

<h1 align="center">gravedecay</h1>

<p align="center">
  <b>Raise any Linux box into an always-on AI dev appliance.<br>
  The box never sleeps — your agents work the graveyard shift.</b> 🪦
</p>

<p align="center">
  <a href="https://github.com/projectmushroom/gravedecay/actions/workflows/ci.yml"><img alt="Main CI status" src="https://github.com/projectmushroom/gravedecay/actions/workflows/ci.yml/badge.svg"></a>
  <a href="https://github.com/projectmushroom/gravedecay/actions/workflows/apple.yml"><img alt="Apple clients CI status" src="https://github.com/projectmushroom/gravedecay/actions/workflows/apple.yml/badge.svg"></a>
  <a href="LICENSE"><img alt="MIT License" src="https://img.shields.io/badge/license-MIT-blue.svg"></a>
</p>

---

gravedecay converts a spare machine (old laptop, mini PC, Steam Machine) into
a personal, tailnet-only AI development server: your repos, databases, and
coding agents live on it 24/7, while your laptops, phones, and tablets become
thin clients. If the machine also games, an optional gaming layer freezes
agent sessions and frees RAM/GPU until you're done.

```
        ┌─────────────────────────────── the box ─────────────────────────────────┐
        │                                                                         │
        │  systemd (native, no containers)           docker (backing svcs only)   │
        │  ├─ gravedecay.service  dashboard :4712    ├─ postgres  127.0.0.1:5432  │
        │  ├─ t3code.service      web UI    :4711    ├─ redis     127.0.0.1:6379  │
        │  ├─ gravedecay-term     ttyd      :4713    └─ playwright browsers       │
        │  ├─ gravedecay-net      flow mon  :4714                                 │
        │  └─ tmux -L agents      persistent claude/codex/shell sessions          │
        │                                                                         │
        │  /srv/dev/{repos,agents,docker,config,logs,scripts,backups,docs}        │
        │  grave <cmd> — one CLI to rule the box                                  │
        └──────────────┬─────────────────────────────┬────────────────────────────┘
                       │ tailscale serve —           │ T3 Connect (opt-in) —
                       │ ONE https origin:           │ outbound relay tunnel or
                       │   /grave = gravedecay       │ notifications-only publish
                       │   /      = T3               │
                       │   /term  = terminal         │
                       │   /net   = gravenet         │
             ┌─────────┴─────────┐             ┌─────┴──────┐
          laptop    iPhone     iPad         official T3 apps
             └─ gravedecay PWA (/grave/) ─┘   (iOS/Android/desktop)
                the system overview & controller      drive the agents
```

## Design principles

1. **Native first.** Agents, web UI, terminal, and dashboard run as plain
   systemd services — agents need real files, real processes, real builds.
   Docker only backs services (postgres, redis, playwright).
2. **Tailnet-only.** Everything binds `127.0.0.1`; the only ways in are
   Tailscale and key-only sshd. Default-deny firewall, no port forwarding,
   ever. One off-by-default exception for the official T3 apps: `grave t3
   connect` — trade-offs in [docs/SECURITY.md](docs/SECURITY.md).
3. **Dev box first; gaming when needed.** `grave gaming` frees RAM/GPU while
   remote access stays up. Opt in with `grave gamewatch on`; stock SteamOS
   starts with it on.
4. **Agent-operated.** Scripts do the deterministic 90 %; a coding agent
   handles the box-specific 10 %. [AGENTS.md](AGENTS.md) is its playbook.
5. **Everything is a file under `$GRAVE_ROOT`** (default `/srv/dev`) —
   snapshot-friendly (btrfs+snapper supported, not required).
6. **Doctor is the contract.** Every invariant the platform relies on is a
   `grave doctor` check; a quirk doctor can't see will silently regress.

## Choose your install

- **Linux appliance** — the complete always-on box above. Quickstart below.
- **Native macOS client** — the *viewer*: a Universal 2 SwiftUI app (macOS
  15+) that discovers graves over Tailscale, with native work state and
  terminal. No services installed; quit it and it's gone. Download the DMG
  from the [latest release](https://github.com/projectmushroom/gravedecay/releases/latest)
  or build per [clients/apple/README.md](clients/apple/README.md) (covers
  Gatekeeper, signing, notarization).
- **macOS companion** — the *server* side: user-scoped LaunchAgents that make
  a Mac itself a grave (dashboard + network monitor; no sudo, no system
  services). Opt into T3 + web terminal with the second form:

  ```sh
  brew install projectmushroom/gravedecay/gravedecay-companion
  gravedecay-mac install            # /grave and /net on the tailnet

  brew install projectmushroom/gravedecay/gravedecay-companion --with-node --with-tmux --with-ttyd
  gravedecay-mac install --agents   # + T3 at / and terminal at /term
  ```

  Details, limits, and coexistence with the native app: [docs/MACOS.md](docs/MACOS.md).
- **Portable Docker workspace** — a reduced work-plane with the same origin
  layout and no host control plane: [docs/DOCKER.md](docs/DOCKER.md).

## Quickstart (Linux appliance)

Requirements: a systemd distro (Arch-family first-class; Debian/Fedora
best-effort), ~8 GB RAM, a free [Tailscale](https://tailscale.com) account.

**The agent way (recommended).** SSH in, install your coding agent, and say:

> Clone `https://github.com/projectmushroom/gravedecay`, read `AGENTS.md`,
> and raise this box. Host profile: `<generic | aws | t2-macbook | steam-machine>`.

The agent runs the ritual, fixes distro quirks, walks you through the two
interactive steps (Tailscale login, T3 pairing), and hands you a passing
`grave doctor`.

**The one-liner** (checks out the latest release; `GRAVEDECAY_CHANNEL=edge`
follows main):

```sh
curl -fsSL https://raw.githubusercontent.com/projectmushroom/gravedecay/master/install.sh | bash -s -- --profile generic
```

**The manual way:**

```sh
git clone https://github.com/projectmushroom/gravedecay
cd gravedecay
./raise.sh --profile generic      # idempotent; uses sudo as needed
grave doctor                      # verify every invariant
```

`raise.sh` is idempotent, so updating *is* re-raising: config is never
clobbered, services and dashboard refresh, doctor verifies the result.

```sh
grave upgrade                     # latest release tag (also in the dashboard UI)
grave upgrade --tag vX.Y.Z        # pin an exact release; --edge follows main
grave uninstall --dry-run         # print the whole teardown, change nothing
grave uninstall [--purge]         # remove platform; --purge also deletes data
```

Uninstall keeps `$GRAVE_ROOT` and docker volumes unless you `--purge`; the
full contract is in [docs/UNINSTALL.md](docs/UNINSTALL.md).

## Connecting a device

Two routes; most setups use both:

**Route A — the tailnet** (dashboard, terminal, files, gravenet). Install
[Tailscale](https://tailscale.com/download) on the device, sign into the same
account as the box, toggle the VPN on (the #1 "it's broken" cause is that
it's off), then open `https://<box>.<tailnet>.ts.net/grave/` and add it to
your Home Screen/Dock — everything on the box is one tap from that PWA. To
use T3's web UI, mint a pairing token from ⚙️ settings on any paired device
and open the printed `/pair` link on the new one.

**Route B — the official T3 apps** ([iOS](https://apps.apple.com/us/app/t3-code-remote-claude-more/id6787819824) ·
[Android](https://play.google.com/store/apps/details?id=com.t3tools.t3code) ·
desktop) drive the agents. Connect them over the tailnet with a pairing
token, or without a VPN on the device via `grave t3 connect full` (managed
relay), or keep tailnet-only transport and still get phone push with
`grave t3 connect publish`. Doctor enforces the declared mode; trade-offs in
[docs/SECURITY.md](docs/SECURITY.md).

## What's on the box

**Dashboard** (`/grave/`) — the system overview and controller, installable
as a PWA. **Work tab:** PRs, Linear issues, CI status, agent token spend,
live agent sessions, repo state. **System tab:** vitals, services, docker,
journal errors, one-tap updates with a release picker. **Launcher tiles** for
T3, terminal, Claude, Codex, GitHub, and a built-in file manager jailed to
`$GRAVE_ROOT`. **⚙️ Settings:** widgets and tiles, pairing tokens, re-auth
flows, T3 Connect, notifications.

**Web terminal** (`/term/`) — ttyd + xterm.js attached to the same
`tmux -L agents` socket as SSH: close the tab, the session lives on; browser,
SSH, and phone reach the *same* session. Drag-select copies via OSC 52
(hold Shift for native browser selection).

**gravenet** (`/net/`) — realtime network view: per-interface RX/TX
sparklines, topology from upstream gateway through the box, DHCP leases,
conntrack count, tailnet peers. One stdlib-only Python daemon, no build step.

**Game mode** (optional) — `grave gaming` freezes agent sessions with the
cgroup v2 freezer (zero CPU, RAM kept, resume mid-thought) and stops
T3/docker; `--kill` for maximum headroom, `--for 2h` auto-restores,
`grave developer` thaws. Tailscale, SSH, and the dashboard always stay up.

**Notifications** — the box pages you (agents finishing or waiting on a
prompt, failing units, failing doctor) via Web Push to the installed PWA
and/or an [ntfy](https://ntfy.sh) topic. Wired but silent until you opt in;
`grave notify "msg"` for scripting. See [docs/NOTIFICATIONS.md](docs/NOTIFICATIONS.md).

**Dev-server preview** — `grave preview 3000` exposes a loopback dev server
at `https://<box>.ts.net:3000`, tailnet-only, at the URL root so HMR and
websockets just work. `grave preview off 3000` stops it.

## Daily driving

```
grave status                     # services, containers, agents, temps, disk
grave doctor                     # verify every platform invariant
grave agents new mybot [dir]     # persistent tmux agent session
grave agents attach mybot        # detach: Ctrl-b d — session survives
grave gaming [--kill] [--for 2h] # 🎮 free resources; optionally auto-restore
grave developer                  # 💻 thaw + restore
grave docker ps|up|down|logs     # stack management
grave preview 3000               # expose a dev server on the tailnet
grave logs t3|dash|term|<unit>   # follow logs
grave update                     # snapshot (if snapper), update pkgs/npm/images
grave backup / restore           # git bundles + configs + docker volumes
grave notify "title" ["body"]    # page your devices
```

## Host profiles

Machine-specific quirks live in `profiles/*.sh`, applied by
`raise.sh --profile <name>`; each flips matching `CHECK_*` doctor flags.

- **generic** — any always-on dev box; masks suspend.
- **aws** — EC2 (Amazon Linux 2023): fills the distro's package gaps; see
  [docs/AWS.md](docs/AWS.md).
- **t2-macbook** — Intel T2 Macs: sleep/lid handling, amdgpu crash workaround.
- **steam-machine** — stock SteamOS (immutable rootfs): durable toolchain in
  `$HOME`, survives OS updates, games alongside; see [docs/STEAMOS.md](docs/STEAMOS.md).

Writing your own is ~20 lines; see `profiles/README.md`.

## Going further

- **Secrets & MCP** — per-integration keys in `$GRAVE_ROOT/config/secrets/`
  reach both Claude and Codex sessions via one systemd drop-in; worked
  example in [docs/SECRETS.md](docs/SECRETS.md).
- **Trusted collaborators** — opt-in multi-user mode with per-user Unix
  identities and project grants: [docs/MULTIUSER.md](docs/MULTIUSER.md).

## Docs

| Doc | What |
|---|---|
| [AGENTS.md](AGENTS.md) | Playbook for the coding agent doing the install |
| [docs/STEAMOS.md](docs/STEAMOS.md) | Raising on stock SteamOS (immutable rootfs): durable toolchain, update-survival |
| [docs/AWS.md](docs/AWS.md) | Raising on EC2 (Amazon Linux 2023): package gaps, `--profile aws`, security group |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | Why native-first, layout, mode model |
| [docs/API.md](docs/API.md) | Versioned, read-only summary contract for thin clients |
| [docs/SECURITY.md](docs/SECURITY.md) | Threat model, tailnet-only, T3 Connect trade-offs, sudoers scope, terminal trust |
| [docs/CLIENTS.md](docs/CLIENTS.md) | Clients: official T3 apps, native Apple shell, and Omarchy widget |
| [clients/apple/README.md](clients/apple/README.md) | Native iOS/macOS client, DMG, signing, and build details |
| [docs/MACOS.md](docs/MACOS.md) | macOS companion, native-app coexistence, and macOS operations |
| [docs/SECRETS.md](docs/SECRETS.md) | Secrets + MCP wiring for agent CLIs |
| [docs/NOTIFICATIONS.md](docs/NOTIFICATIONS.md) | Web Push + ntfy: agents, failing units, and doctor page your phone |
| [docs/PORTS.md](docs/PORTS.md) | Every port, documented or it doesn't exist |
| [docs/RECOVERY.md](docs/RECOVERY.md) | Backup/restore procedures |
| [docs/UNINSTALL.md](docs/UNINSTALL.md) | Unraising the box: what is removed, kept, and deliberately untouched |

## License

MIT. Daemons in the dirt, shipping while you sleep. 🪦
