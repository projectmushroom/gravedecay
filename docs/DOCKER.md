# Portable Docker workspace

This is a reduced **work-plane**, for a laptop, small server, or disposable
developer host. The normal `raise.sh` appliance remains the recommended full
installation: it owns system services, Tailscale, firewall policy, backups,
and optional gaming mode. The portable stack deliberately does none of those.

It runs T3 Code, current Claude Code and Codex CLIs, tmux-backed web terminal,
and the work dashboard behind one same-origin gateway:

| Path | Service |
|---|---|
| `/` | T3 Code |
| `/grave/` | gravedecay work dashboard |
| `/term/` | web terminal |

## Start

Docker Engine **28.0.0 or newer is required** for the documented loopback-only
port guarantee. Older Engines could expose a `127.0.0.1` published port to
same-L2 hosts; upgrade, or enforce an equivalent host firewall deny before
using this stack.

```sh
cd docker/portable
COMPOSE_PROJECT_NAME=grave PORT=4711 docker compose up -d --build
curl -f http://127.0.0.1:4711/healthz
```

Only the nginx gateway publishes a host port, and Compose pins it to
`127.0.0.1`. App ports are private to the Compose network. Do not put this
port behind a public or LAN proxy: the dashboard trusts requests without a
`Tailscale-User-Login` header as local. Keep it on loopback, or front it with
host Tailscale Serve (or another trusted access-control proxy that supplies
identity headers).

For a tailnet entry point, run Tailscale on the **host**, never in this
container:

```sh
tailscale serve --bg --https=443 http://127.0.0.1:4711
```

Serve forwards the identity headers through nginx. It also preserves HTTPS for
pairing URLs; direct localhost use produces an `http://localhost:…` pairing
URL instead. Set `GRAVEDECAY_ALLOWED_USERS=you@example.com` in the environment
when the dashboard should permit pairing-token creation or file writes through
Serve; absent that value, remote viewers are read-only.
`GRAVEDECAY_ALLOWED_USERS` gates dashboard mutations only — it does **not**
authenticate T3 or ttyd. On a shared tailnet, restrict node reachability with
Tailscale ACLs; `/term/` is an interactive shell for anyone who can reach it.

## Persistence, login, and instances

The default named volumes are project-scoped:

- `workspace` holds repos, T3 state, dashboard settings, and logs.
- `agent-home` holds Claude/Codex/GitHub credentials and is intentionally
  mounted at `/home/grave`, **outside** `GRAVE_ROOT=/workspace`. The dashboard
  file manager therefore cannot browse agent credentials.

Open `/term/?arg=auth-claude` or `/term/?arg=auth-codex` and complete the CLI
login there. `COMPOSE_PROJECT_NAME=grave2 PORT=4712 docker compose up -d`
creates a fully separate set of volumes and gateway port. Back up named volumes
before deleting a project with `docker compose down -v`.

Volumes preserve files, settings, and credentials, but an app container
restart/recreate kills its in-flight tmux and agent processes. Treat rebuilds
and updates as a maintenance stop; this reduced work-plane does not provide the
bare-metal appliance's process-survival contract.

The image tracks the latest T3, Claude Code, and Codex at build time (matching
the bare-metal maintenance contract), with `git`, `gh`, `jq`, `rg`, and the
Node image's existing compiler toolchain as a compact agent baseline. Rebuild
to update. For SDKs or project-specific dependencies, derive a tiny image from
`app`'s Dockerfile instead of mounting the host toolchain.

## Deliberate limits

There is no Docker socket, privileged mode, host PID/network namespace,
systemd, or in-container Tailscale. That means no host Docker controls,
journald/systemd status, firewall, backup, reboot, update, or gaming controls;
the dashboard is explicitly in portable mode and only offers work/session
features plus safe T3 pairing. Excluding `/var/run/docker.sock` matters: access
to it is effectively host-root access, which would defeat this boundary.

The Engine 28 requirement follows [Docker's localhost port-publishing
warning](https://docs.docker.com/engine/network/port-publishing/).
