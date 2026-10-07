<p align="center">
  <img src="assets/gravedecay-skull.svg" width="128" alt="gravedecay skull logo">
</p>

<h1 align="center">gravedecay</h1>

<p align="center"><b>A spare Linux box, raised as the night shift for your repos.</b> 🪦</p>

<p align="center">
  <a href="https://github.com/projectmushroom/gravedecay/actions/workflows/ci.yml"><img alt="CI" src="https://github.com/projectmushroom/gravedecay/actions/workflows/ci.yml/badge.svg"></a>
  <a href="https://github.com/projectmushroom/gravedecay/actions/workflows/browser.yml"><img alt="Browser matrix" src="https://github.com/projectmushroom/gravedecay/actions/workflows/browser.yml/badge.svg"></a>
  <a href="https://github.com/projectmushroom/gravedecay/actions/workflows/apple.yml"><img alt="Apple clients" src="https://github.com/projectmushroom/gravedecay/actions/workflows/apple.yml/badge.svg"></a>
  <a href="LICENSE"><img alt="MIT License" src="https://img.shields.io/badge/license-MIT-blue.svg"></a>
</p>

---

## What it is

A spare Linux box becomes a private night shift for your repositories.
Coding agents work on it while you sleep, each run in its own worktree.
The box reruns your own test command afterwards, next to your real local
services (Postgres, Redis, whatever else lives on it). Your phone gets a
result card in the morning: verdict, diff, cost, and a live link to the
running branch. Everything stays on hardware you own, behind your tailnet.

It is for developers with a Claude or Codex subscription and a Linux machine
that can stay on: an old laptop, a mini PC, a Steam Machine, a VM.

## Dusk, night, dawn

*The box never sleeps so that you can.*

**Evening.** Queue a job from the CLI (`grave agents run`) or from the
dashboard's Linear **Work on this** button. The prompt is copied at that
moment; the schedule can be once, nightly or weekly.

**Night.** Each run gets a fresh worktree and an `agent/job-…` branch from the
committed HEAD. When the agent exits, the runner reruns the repository's test
entry point as it was at the base commit, a command the agent did not choose.
A game on the box freezes the agents in place (torpor); a run cut short by it
is requeued once, and a job that comes due while gaming waits. A Claude usage
limit defers the job to its reset time.

**Morning.** One card per run on the dashboard: verdict, change summary, each
check with its source and exit code, an amber line when tests were trimmed,
the diff, an **Open live** button for the running branch, and a cost line.
The same event reaches your phone as a push.

## What you need

- **Hardware.** Any machine that can stay on, about 8 GB RAM. Intel T2 Macs,
  EC2 and stock SteamOS have their own host profiles.
- **OS.** A systemd Linux. Arch-family (Arch, CachyOS, Omarchy) is first
  class; Debian/Ubuntu, Fedora and openSUSE Leap are best effort.
- **A Tailscale account.** Free tier is enough; every device that reaches the
  box joins the same tailnet.
- **A provider login.** Claude Code or Codex, signed in on the box as the
  appliance owner, with that account's permission rules opened for unattended
  work; a fresh login refuses to edit or install anything headless
  ([docs/SCHEDULES.md](docs/SCHEDULES.md#provider-command-and-sandbox)).

The raise installs packages, a default-deny firewall, systemd services, Docker
for Postgres and Redis, one `tailscale serve` origin, and a dedicated owner
account with scoped passwordless sudo. `grave uninstall` reverses all of it
and keeps your repos, agents, secrets and backups unless you pass `--purge`
([docs/UNINSTALL.md](docs/UNINSTALL.md)).

## Raise it

```sh
git clone https://github.com/projectmushroom/gravedecay
cd gravedecay
./raise.sh --profile generic      # idempotent; asks which account owns the box
grave doctor                      # every check must pass
```

Profiles: `generic`, `aws`, `t2-macbook`, `steam-machine`. Two steps need a
human: `sudo tailscale up --ssh` prints a login URL, and a T3 pairing link
(dashboard, System, Connections & integrations) enrols your phone. Rerun
`./raise.sh` after the Tailscale login, then `grave doctor` again.

The alternative is to let an agent raise it. SSH in, install Claude Code or
Codex, and say: *Clone https://github.com/projectmushroom/gravedecay, read
AGENTS.md, and raise this box with profile generic.* [AGENTS.md](AGENTS.md)
is the playbook; the agent fixes distro quirks and hands you a passing doctor.

Updating is re-raising: `grave upgrade` pulls the latest release and reruns
the ritual without touching your config.

## First result: raise the dead

*A repository nobody has built in a year makes a good first corpse.*

Clone it under `$GRAVE_ROOT/repos` and queue the shipped prompt from the
gravedecay checkout:

```sh
git clone https://github.com/you/dead-repo "$GRAVE_ROOT/repos/dead-repo"
grave agents run resurrect --repo dead-repo --prompt-file prompts/resurrect.txt
```

[prompts/resurrect.txt](prompts/resurrect.txt) tells the agent to establish
what builds and what fails, reconstruct the documented dev setup (Node and
Python first), attempt a bounded repair, run the checks, commit, and end with
a report of what is still dead. It never merges, pushes or deploys.

The run starts on the next scheduler scan (`--at 02:00` makes it nightly).
In the morning `grave agents jobs` shows one line per job:

```
resurrect            claude dead-repo            next —  enabled  last succeeded/ready-for-review
```

The dashboard's **Overnight report** shows the card. It leads with the
verdict: green `ready-for-review` (the agent exited cleanly, left commits or
edits, and every check passed), red `checks-failed` or `provider-failed`,
amber `unverified` when no check ran. Below it: commits, files and lines
changed; each check with its command, `source` (`owner` or `base:<sha>`),
exit code and duration; the amber **Test changes** line when tests were
deleted, skipped or trimmed; the estimated cost; the output tail, which ends
with the agent's report; and the diff against the base.

To see the branch running, add `--serve` when queuing:

```sh
grave agents run resurrect --repo dead-repo --prompt-file prompts/resurrect.txt \
  --serve 'vite --host 127.0.0.1 --port $PORT --strictPort'
```

After a `ready-for-review` run the box starts that command in the worktree,
maps it over the tailnet, and the card gains **Open live**, valid for 24 hours
or until the next run. Details and the verdict table are in
[docs/SCHEDULES.md](docs/SCHEDULES.md).

## What lies beneath

- **Dashboard** (`/grave/`): the overview and controller, installable as a
  PWA; Work tab for runs, PRs, Linear issues, CI and spend; System tab for
  vitals, services, updates ([docs/DASHBOARD.md](docs/DASHBOARD.md)).
- **Terminal** (`/term/`): ttyd on the same `tmux -L agents` socket as SSH;
  close the tab and the session lives on ([docs/TERMINAL.md](docs/TERMINAL.md)).
- **Preview**: `grave preview 3000` exposes a loopback dev server at
  `https://<box>.ts.net:3000` on the tailnet; jobs get the same through `--serve`.
- **gravenet** (`/net/`): live network view, per-interface traffic, tailnet
  peers, one stdlib Python daemon.
- **Gravekeeper**: `grave keeper ask "<question>"` runs a read-only agent over
  the box's own state, logs and docs; it diagnoses, it does not change anything.
- **Notifications**: Web Push to the installed PWA and/or an ntfy topic, for
  finished runs, failing units and a failing doctor
  ([docs/NOTIFICATIONS.md](docs/NOTIFICATIONS.md)).
- **Gaming mode**: `grave gaming` freezes agent sessions with the cgroup
  freezer and stops T3 and Docker; `grave developer` thaws; SteamOS boxes can
  switch automatically.
- **Backups**: a nightly timer bundles repos, configs and Docker volumes;
  doctor enforces freshness; `grave restore` puts them back
  ([docs/RECOVERY.md](docs/RECOVERY.md)).

## Tending the grave

```
grave status                              # services, containers, agents, temps, disk
grave doctor                              # verify every platform invariant
grave agents run <job> --repo <r> --prompt-file <f> [--at 02:00] [--check '<cmd>'] [--serve '<cmd>']
grave agents jobs                         # schedules and last verdicts; jobs cancel <job> stops one
grave agents new <name> --repo <r>        # interactive session in an isolated worktree
grave agents attach <name>                # detach with Ctrl-b d; the session survives
grave agents prune                        # drop clean, merged, idle worktrees
grave preview 3000                        # expose a loopback dev server on the tailnet
grave gaming [--kill] [--for 2h]          # torpor: freeze agents, free RAM and GPU
grave developer                           # thaw and restore
grave notify "title" ["body"]             # page your devices
grave logs t3|dash|term|<unit>            # follow logs
grave upgrade                             # latest release; --edge follows master
grave backup                              # bundles, configs, volumes; grave restore undoes
```

## What stays buried

- **Tailnet only.** The ways in are Tailscale and key-only SSH. One
  `tailscale serve` origin carries the dashboard, T3, terminal and gravenet;
  the official T3 apps' relay is the single opt-in exception
  ([docs/SECURITY.md](docs/SECURITY.md)).
- **Loopback binds.** Every service, database and preview listens on
  `127.0.0.1`; a serve command that binds wider is killed and never mapped.
- **Default-deny firewall.** ufw or firewalld, SSH and `tailscale0` allowed,
  nothing else, no port forwarding.
- **Doctor is the contract.** Every invariant above, and every run record,
  check source, serve mapping and the no-bypass-flag provider table, is a
  `grave doctor` check. A quirk doctor cannot see will regress.
- **The honest limit.** Agents and the runner share one root-equivalent owner
  account (scoped sudoers), and checks run agent-written code as that owner,
  inside the test process; a live preview keeps that code running for up to a
  day. Read the verdict as checks run by the runner with a command the agent
  did not choose, then read the diff.

## Neighbouring plots

| Tool | What it does | What gravedecay does instead |
| --- | --- | --- |
| [gatekeep](https://github.com/SagnikKK1/gatekeep) | Hooks, CLI and a GitHub Action that stop an agent from calling work done when tests were deleted, skipped or weakened, diffed against a session-start snapshot. | Shows the same patterns on the card as an amber, informational line, never as the verdict; add gatekeep as a `--check` to make it one. |
| [discipline](https://github.com/orieg/discipline) | A GitHub Action with a test-integrity gate: deleted test files, unconditional skip/only/todo markers and assertion reductions fail the PR unless a label says the change is intended. | Runs on the box before a PR exists, with your own test command; [docs/REQUIRED-CHECKS.md](docs/REQUIRED-CHECKS.md) shows how to require either tool on the repository. |
| protect-the-oracle recipe | Freeze the tests at the base commit, lock fixtures by hash and cap skips in CI, so the oracle grading the agent is not the one it edited. | Pins the check command to the base commit the same way, but runs it in the agent's worktree where tests are writable; the pin stops a swapped command, not an edited test. |
| [Paseo](https://paseo.sh) | Self-hosted daemon that runs Claude Code, Codex, OpenCode and others on your machines, driven from phone, desktop and web, with worktrees, push and an optional relay. | Leaves live-session control to T3 and adds the unattended schedule, the runner-executed checks, the morning card and the system contract around the box. |
| [Open Session](https://github.com/tellahq/opensession) | Self-hosted server that drives sessions in worktrees or sandboxes with Slack, Linear, Plain and GitHub intake, diff and PR review, and several subscriptions. | A single-owner appliance: firewall, services, backups and doctor come with it; Linear intake is one button and GitHub PR intake is left out on purpose. |
| Cursor cloud agents | Each agent in an isolated cloud VM that clones from GitHub, drives a browser and opens a PR. | The same shape on hardware you own, next to your real database and private services, with no per-day cap, no vendor relay, and a live preview of the branch instead of a recording. |

## Other plots

<details>
<summary>macOS app, companion, portable Docker, multi-user</summary>

- **Standalone macOS app.** A Universal SwiftUI app that hosts this Mac as a
  grave and reaches your other graves; DMG on the
  [latest release](https://github.com/projectmushroom/gravedecay/releases/latest),
  setup in [docs/MACOS.md](docs/MACOS.md#native-app-hosting).
- **Classic macOS companion.** User-scoped LaunchAgents, no sudo:
  `brew install projectmushroom/gravedecay/gravedecay-companion` then
  `gravedecay-mac install` ([docs/MACOS.md](docs/MACOS.md)).
- **Portable Docker workspace.** The same origin layout with no host control
  plane, for a laptop or a disposable host ([docs/DOCKER.md](docs/DOCKER.md)).
- **Multi-user mode.** Opt-in per-user Unix identities and project grants for
  a few trusted collaborators; frozen, not expanding
  ([docs/MULTIUSER.md](docs/MULTIUSER.md)).
- **Clients.** The official T3 apps, the native Apple shell and the Omarchy
  widget ([docs/CLIENTS.md](docs/CLIENTS.md)).

</details>

## Docs

| Doc | What |
| --- | --- |
| [AGENTS.md](AGENTS.md) | Playbook for the agent that raises the box |
| [CONTRIBUTING.md](CONTRIBUTING.md) | Running the suites in containers; prompts, check detection and host profiles |
| [docs/SCHEDULES.md](docs/SCHEDULES.md) | Scheduled runs, the result package, verdicts, live preview |
| [docs/REQUIRED-CHECKS.md](docs/REQUIRED-CHECKS.md) | Making a test-integrity workflow required on a GitHub repository |
| [docs/DASHBOARD.md](docs/DASHBOARD.md) | Dashboard layout and the Overnight report |
| [docs/DISPATCH.md](docs/DISPATCH.md) | Starting work from a Linear issue |
| [docs/NOTIFICATIONS.md](docs/NOTIFICATIONS.md) | Web Push and ntfy |
| [docs/SECURITY.md](docs/SECURITY.md) | Threat model, sudoers scope, T3 Connect, the shared account |
| [docs/INSTALL.md](docs/INSTALL.md) | Choosing the appliance account |
| [docs/PORTS.md](docs/PORTS.md) | Every port, documented or it does not exist |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | Native first, layout, mode model |
| [docs/API.md](docs/API.md) | Read-only summary contract for thin clients |
| [docs/TERMINAL.md](docs/TERMINAL.md) | The web terminal |
| [docs/SECRETS.md](docs/SECRETS.md) | Secrets and MCP wiring for agent CLIs |
| [docs/RECOVERY.md](docs/RECOVERY.md) | Backup and restore |
| [docs/UNINSTALL.md](docs/UNINSTALL.md) | Unraising: removed, kept, untouched |
| [docs/BENCHMARK.md](docs/BENCHMARK.md) | Development speed scores |
| [docs/GRAVEYARD.md](docs/GRAVEYARD.md) | Several graves from one overview |
| [docs/CLIENTS.md](docs/CLIENTS.md) | Official T3 apps, Apple shell, Omarchy widget |
| [docs/MACOS.md](docs/MACOS.md) | macOS app and companion |
| [docs/DOCKER.md](docs/DOCKER.md) | Portable Docker workspace |
| [docs/MULTIUSER.md](docs/MULTIUSER.md) | Multi-user identity and authorization |
| [docs/STEAMOS.md](docs/STEAMOS.md) | Stock SteamOS: durable toolchain, update survival |
| [docs/AWS.md](docs/AWS.md) | EC2 and Amazon Linux 2023 |
| [docs/VM.md](docs/VM.md) | Plain VMs and openSUSE Leap |
| [docs/NEXT.md](docs/NEXT.md) | Direction and what comes next |
| [profiles/README.md](profiles/README.md) | Writing a host profile |
| [clients/apple/README.md](clients/apple/README.md) | Native iOS and macOS client build |

## Licence

MIT. Daemons in the dirt, shipping while you sleep. 🪦
