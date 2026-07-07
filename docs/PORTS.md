# Ports

Every listening port on the box. If it's not in this table, it shouldn't be
listening — add a row in the same commit that adds a listener.

| Port | Bind | Service | Exposed as |
|---|---|---|---|
| 22 | all | sshd (key-only) | LAN + tailnet (tailnet 22 is intercepted by Tailscale SSH) |
| 4711 | 127.0.0.1 | t3code | `tailscale serve` → https `/` |
| 4712 | 127.0.0.1 | gravedecay | `tailscale serve` → https `/grave` (the entry point — install the PWA from here) |
| 4713 | 127.0.0.1 | gravedecay-term (ttyd) | `tailscale serve` → https `/term` (shell for the whole tailnet — see SECURITY.md) |
| 5432 | 127.0.0.1 | core-postgres | loopback only |
| 6379 | 127.0.0.1 | core-redis | loopback only |
| 3050 | 127.0.0.1 | browsers-playwright | loopback only |
| 3000–3999 | 127.0.0.1 | dev-server previews (your projects) | opt-in per port via `grave preview <port>` → https `:<port>` on the tailnet |

The 3000–3999 range is the sandbox for `grave preview` (config: `PREVIEW_RANGE`).
Dev servers still bind loopback; `grave preview <port>` runs `tailscale serve
--https=<port>` so the project is reachable at `https://<box>.ts.net:<port>` —
served at the port root, not a path, so HMR/websockets/absolute URLs work with
no per-project config. Previews persist until `grave preview off <port>`.

Audit: `sudo ss -tlnp` and `sudo docker ps --format '{{.Names}} {{.Ports}}'`;
`grave preview list` for what's currently exposed.
