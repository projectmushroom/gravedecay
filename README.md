# gravedecay

<img src="assets/gravedecay.png" width="128" align="right" alt="gravedecay logo">

**Turn any Linux box into an always-on AI dev appliance. The box never sleeps — your agents work the graveyard shift.** 🪦

gravedecay converts a spare machine (old laptop, mini PC, Steam Machine) into a
personal, tailnet-only AI development server: your repos, databases, and coding
agents live on it 24/7, while your laptops, phones, and tablets become thin
clients. Gaming keeps priority — one command flips the box between *developer*
and *gaming* mode.

```
        ┌─────────────────────────────── the box ────────────────────────────────┐
        │                                                                        │
        │  systemd (native, no containers)          docker (backing svcs only)   │
        │  ├─ t3code.service      web UI :4711      ├─ postgres  127.0.0.1:5432  │
        │  │    └─ spawns claude / codex CLIs       ├─ redis     127.0.0.1:6379  │
        │  ├─ gravedecay.service   dashboard :4712   └─ playwright browsers       │
        │  └─ tmux -L agents      persistent CLI agent sessions                  │
        │                                                                        │
        │  /srv/dev/{repos,agents,docker,config,logs,scripts,backups,docs}       │
        │  grave <cmd> — one CLI to rule the box                                 │
        └────────────────────────────┬───────────────────────────────────────────┘
                                     │ tailscale serve (HTTPS, tailnet-only)
                                     │ one origin: /dash = gravedecay, / = T3
                     ┌───────────────┼───────────────┐
                  laptop           iPhone          iPad
                     └── gravedecay PWA (/dash/) — THE entry point ──┘
```

![the gravedecay dashboard — terminal skin](assets/dashboard.png)

**gravedecay is the front door.** Install the PWA / macOS web app from
`https://<box>.<tailnet>.ts.net/dash/` — it launches every app on the box
(T3 Code today, whatever you mount tomorrow via `GRAVEDECAY_APPS` tiles), all
same-origin so the hop never leaves the installed app. Inside T3, a tiny
translucent gauge pill (bottom-left, standalone-mode only — regular browser
tabs never see it) brings you back to the dashboard.

## Design principles

1. **Native first.** Agent CLIs, the web UI, and the dashboard run as plain
   systemd services on the host — agents need real files, real processes, real
   builds. Docker is only for backing services (Postgres, Redis, browsers).
2. **Tailnet-only.** Everything binds `127.0.0.1`; the only ways in are
   Tailscale (`tailscale serve` for HTTPS UIs, Tailscale SSH as fallback) and
   key-only sshd. Firewall is default-deny. No port forwarding, ever.
3. **Gaming keeps priority.** `grave gaming` stops every dev service and
   container, freeing RAM/GPU; remote access stays up. `grave developer`
   brings it all back.
4. **Agent-operated.** The scripts do the deterministic 90 %; a coding agent
   (Claude Code, Codex, …) handles the box-specific 10 %. `AGENTS.md` is the
   playbook you point your agent at.
5. **Everything is a file under `$GRAVE_ROOT`** (default `/srv/dev`) — repos,
   configs, logs, backups, docs. Snapshot-friendly (btrfs+snapper supported,
   not required).

## Quickstart

### The agent way (recommended)

SSH into the fresh box, install your coding agent, and say:

> Clone `https://github.com/projectmushroom/gravedecay`, read `AGENTS.md`,
> and raise this box. Host profile: `<generic | t2-macbook | steam-machine>`.

The agent runs the ritual, fixes distro quirks, walks you through the two
interactive steps (Tailscale login, T3 pairing), and hands you a passing
`grave doctor`.

### The manual way

```sh
git clone https://github.com/projectmushroom/gravedecay
cd gravedecay
./raise.sh --profile generic      # idempotent; uses sudo as needed
grave doctor                      # verify every invariant
```

Requirements: a systemd-based distro (Arch-family is first-class; Debian/Fedora
best-effort), ~8 GB RAM, and a [Tailscale](https://tailscale.com) account
(free tier is fine).

## What raise.sh does

Each step is idempotent — rerun it any time:

1. Installs packages: `tmux git curl jq python docker nodejs npm` (+ `ufw` on
   Arch/Debian) via pacman/apt/dnf.
2. Lays out `$GRAVE_ROOT` and symlinks `~/Projects → $GRAVE_ROOT/repos`.
3. Installs the `grave` CLI to `/usr/local/bin` and its config to
   `/etc/gravedecay/grave.conf`.
4. Installs **gravedecay** (single-file stdlib-Python dashboard, mobile PWA)
   and **T3 Code** (`npm i -g t3`, web UI that spawns claude/codex sessions)
   as systemd services on `127.0.0.1`.
5. Prepares Docker: `devnet` network, `core` stack (Postgres 17 + Redis 8,
   loopback-bound, random password generated into `.env`) and a `browsers`
   stack (Playwright).
6. Firewall: default-deny incoming, allow SSH + the `tailscale0` interface
   (SSH is allowed *before* enabling — you won't be locked out).
7. Installs a scoped sudoers file so `grave` and gravedecay's action buttons
   work without a password (see `docs/SECURITY.md`).
8. Applies the host profile (quirks like "never suspend", GPU pinning).
9. Publishes both UIs on your tailnet if Tailscale is up:
   T3 on `https://<box>.<tailnet>.ts.net`, gravedecay on `:8443`.
10. Runs `grave doctor`.

## Daily driving

```
grave status                     # services, containers, agents, temps, disk
grave doctor                     # verify every platform invariant
grave gaming | grave developer   # 🎮 / 💻 mode flip
grave agents new mybot [dir]     # persistent tmux agent session
grave agents attach mybot        # detach: Ctrl-b d — session survives
grave docker ps|up|down|logs     # stack management
grave logs t3|<unit>|<container> # follow logs
grave update                     # snapshot (if snapper), update pkgs/npm/images
grave backup                     # git bundles + configs + docker volumes
grave restore [ts]               # list / restore backups
```

The dashboard (add to your phone's home screen) shows mode, services,
containers, agent sessions, repo status, journal errors, temps — with buttons
for mode flips, doctor, and T3 restart, gated to your Tailscale identity.
A ⚙️ settings panel (same identity gating) lets you show/hide/reorder the
widgets, hide launcher tiles, add custom tiles, and set the refresh rate —
stored in `$GRAVE_ROOT/config/gravedecay-settings.json`.

## Web terminal

`https://<box>/term/` is a full terminal in the browser (ttyd + xterm.js)
attached to the same `tmux -L agents` socket as `grave agents` — sessions
survive closed tabs, and the browser, SSH, and phone all reach the SAME
session. The launcher ships three tiles: 🖥️ Terminal, 🤖 Claude, 🧠 Codex —
the agent tiles drop you straight into a persistent `claude` / `codex` CLI
session. TUIs render pixel-correct (xterm.js is what VS Code uses); on
desktop it feels native, on iOS the soft keyboard lacks Esc/Ctrl so treat it
as a quick-look tool there.

## Host profiles

Machine-specific quirks live in `profiles/*.sh`, applied once by `raise.sh
--profile <name>`:

- **generic** — any always-on box; optionally masks suspend (`MASK_SLEEP=1`).
- **t2-macbook** — Intel Macs with the T2 chip: masks sleep, ignores lid
  close, pins amdgpu to a fixed DPM state (dGPU crash workaround).
- **steam-machine** — always-on + gaming coexistence for gamescope-session
  machines (work in progress).

Writing your own is ~20 lines; see `profiles/README.md`.

## Secrets & MCP for your agents

Per-service secrets live in `$GRAVE_ROOT/config/secrets/*.env` (git-ignored,
`chmod 600`) and are fed to T3-spawned agent sessions via a systemd drop-in —
so the same Linear/GitHub/whatever API key reaches both your Claude and Codex
sessions. The pattern (with worked Linear MCP example) is in
`docs/SECRETS.md`.

## Docs

| Doc | What |
|---|---|
| [AGENTS.md](AGENTS.md) | Playbook for the coding agent doing the install |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | Why native-first, layout, mode model |
| [docs/SECURITY.md](docs/SECURITY.md) | Threat model, tailnet-only, sudoers scope |
| [docs/SECRETS.md](docs/SECRETS.md) | Secrets + MCP wiring for agent CLIs |
| [docs/PORTS.md](docs/PORTS.md) | Every port, documented or it doesn't exist |
| [docs/RECOVERY.md](docs/RECOVERY.md) | Backup/restore procedures |

## License

MIT. The name is the vibe: quiet box in the corner, daemons in the dirt,
shipping while you sleep. 🪦
