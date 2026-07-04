# Security model

## Threat model

A personal box on a home LAN, reachable only over a personal tailnet. The
goal: LAN compromise or a stray port-forward exposes nothing; a lost laptop
or phone can be evicted from the tailnet centrally.

## Rules

1. **Nothing listens beyond loopback.** Every service binds `127.0.0.1`.
   Docker containers publish to `127.0.0.1:` only — `grave doctor` fails if
   any container publishes on `0.0.0.0`.
2. **Tailscale is the front door.** `tailscale serve` terminates HTTPS on the
   tailnet and proxies to loopback. Identity comes from the tailnet — serve
   injects `Tailscale-User-Login`, which gravedecay checks before allowing
   action buttons (`GRAVEDECAY_ALLOWED_USERS`).
3. **SSH**: key-only (`PasswordAuthentication no` — doctor-enforced),
   plus Tailscale SSH as a fallback door. Note: Tailscale SSH intercepts
   port 22 *over the tailnet*; plain sshd remains reachable via LAN IPs only.
4. **Firewall default-deny incoming**; allow ssh + the `tailscale0`
   interface. `raise.sh` allows SSH *before* enabling so you're not locked out.
5. **Secrets** live in `$GRAVE_ROOT/config/secrets/*.env`, mode 600,
   git-ignored, delivered via systemd `EnvironmentFile=` — never in unit
   files, shell configs, or agent JSON configs (use `${VAR}` expansion).

## The sudoers file

`raise.sh` installs `/etc/sudoers.d/50-gravedecay`: NOPASSWD for your user on
`systemctl`, `docker`, `grave`, `journalctl`, `ufw`, `snapper`, `sshd -T` —
what `grave` and gravedecay's action buttons need. This is effectively
root-equivalent for *your* user (systemctl alone gets you there); the point is
convenience for a single-human box, not privilege separation. If your box has
other human users, tighten it.

## The web terminal

`/term` (ttyd → the shared `tmux -L agents` socket) is an interactive shell as
your user for **anyone who can reach it** — ttyd does not check the
`Tailscale-User-Login` header. On a personal tailnet this is the same trust
you already extend via Tailscale SSH; on a shared tailnet, restrict who can
reach this node with Tailscale ACLs or disable `gravedecay-term`.

## What gaming mode does NOT change

Remote access (tailscaled, sshd), the firewall, and gravedecay stay up in
gaming mode. You can always get back in.
