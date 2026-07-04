# Ports

Every listening port on the box. If it's not in this table, it shouldn't be
listening — add a row in the same commit that adds a listener.

| Port | Bind | Service | Exposed as |
|---|---|---|---|
| 22 | all | sshd (key-only) | LAN + tailnet (tailnet 22 is intercepted by Tailscale SSH) |
| 4711 | 127.0.0.1 | t3code | `tailscale serve` → https 443 |
| 4712 | 127.0.0.1 | gravedash | `tailscale serve` → https 8443 |
| 5432 | 127.0.0.1 | core-postgres | loopback only |
| 6379 | 127.0.0.1 | core-redis | loopback only |
| 3050 | 127.0.0.1 | browsers-playwright | loopback only |

Audit: `sudo ss -tlnp` and `sudo docker ps --format '{{.Names}} {{.Ports}}'`.
