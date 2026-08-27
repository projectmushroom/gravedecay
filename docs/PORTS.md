# Ports

Every listening port on the box. If it's not in this table, it shouldn't be
listening — add a row in the same commit that adds a listener.

| Port | Bind | Service | Exposed as |
|---|---|---|---|
| 22 | all | sshd (key-only) | LAN + tailnet (tailnet 22 is intercepted by Tailscale SSH) |
| 4710 | 127.0.0.1 | multi-user identity gateway (opt-in) | `tailscale serve` → HTTPS origin |
| 4711 | 127.0.0.1 | t3code (Linux systemd, or macOS `io.gravedecay.t3` LaunchAgent in opt-in agents mode) | `tailscale serve` → https `/` (disabled in multi-user mode) |
| 4712 | 127.0.0.1 | gravedecay (Linux systemd, macOS legacy user LaunchAgent, or explicitly enabled native Mac app process) | `tailscale serve` → https `/grave`; native app does not change Serve and refuses collisions; multi-user admin actions only via root gateway |
| 4713 | 127.0.0.1 | gravedecay-term (ttyd, custom clipboard-capable frontend — see TERMINAL.md; Linux systemd, or macOS `io.gravedecay.term` LaunchAgent in opt-in agents mode) | `tailscale serve` → https `/term` (disabled in multi-user mode) |
| 4714 | 127.0.0.1 | gravedecay-net (gravenet — Linux systemd or macOS user LaunchAgent) | `tailscale serve` → https `/net` (widen bind via `GRAVENET_BIND` drop-in only if LAN clients should load it directly) |
| `${PORT:-4711}` | 127.0.0.1 | portable Compose nginx gateway (optional) | same-origin `/`, `/grave/`, `/term/`; choose a distinct `PORT` per Compose project |
| 5432 | 127.0.0.1 | core-postgres | loopback only |
| 6379 | 127.0.0.1 | core-redis | loopback only |
| 3050 | 127.0.0.1 | browsers-playwright | loopback only |
| 3000–3999 | 127.0.0.1 | dev-server previews (your projects) | opt-in per port via `grave preview <port>` → https `:<port>` on the tailnet |
| 4810–4909 | 127.0.0.1 | per-workspace T3 instances | identity gateway only |
| 4910–5009 | 127.0.0.1 | per-workspace terminals | identity gateway only |
| 5010–5109 | 127.0.0.1 | per-workspace dashboards | identity gateway only |

Multi-user mode points Serve at a root-only, randomly generated capability
path on port 4710. The gateway strips that path and selects one of the fixed
loopback ports from the workspace registry; callers cannot select a backend.
Its root-owned nftables boundary rejects any non-root local TCP connection to
4711–4713 and 4810–5109 (for both IPv4 and IPv6), leaving the root gateway as
the only backend caller. `grave doctor` verifies the persistent boundary and
that legacy T3/terminal remain disabled.

The 3000–3999 range is the sandbox for `grave preview` (config: `PREVIEW_RANGE`).
Dev servers still bind loopback; `grave preview <port>` runs `tailscale serve
--https=<port>` so the project is reachable at `https://<box>.ts.net:<port>` —
served at the port root, not a path, so HMR/websockets/absolute URLs work with
no per-project config. Previews persist until `grave preview off <port>`.

On macOS the dashboard may read containers from the active standard Docker CLI
context, but it creates no Docker listener or backing port. Any existing
container port remains its runtime's responsibility and must be loopback-only
before it is deliberately published over the tailnet.

T3 Connect (`grave t3 connect full`) adds **no listener**: the T3 server
opens an outbound `cloudflared` tunnel to upstream's relay. It widens the
box's reachability without touching this table — the invariant for it lives
in `grave doctor` (declared mode vs. link/tunnel state), and the trust trade
is documented in SECURITY.md.

Audit: `sudo ss -tlnp` and `sudo docker ps --format '{{.Names}} {{.Ports}}'`;
`grave preview list` for what's currently exposed.
