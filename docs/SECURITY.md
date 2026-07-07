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

## The file manager

The dashboard's 📁 Files modal browses, uploads, downloads, and edits files
so you can move projects onto the box from a browser. It is confined:

- **Jailed to `$GRAVE_ROOT`.** Every request path is `realpath`'d and
  prefix-checked against the root; `..` and symlinks that resolve outside the
  tree are refused (so the `repos/gravedecay` recovery symlink is invisible
  here — edit that repo over git/T3).
- **Gated like the action buttons.** Reads *and* writes require
  `Tailscale-User-Login ∈ GRAVEDECAY_ALLOWED_USERS`; listing a filesystem is
  as sensitive as changing it. Localhost (no header) stays trusted.
- **The appliance's own secret store is hidden.** `$GRAVE_ROOT/config/secrets/`
  is excluded from listing, download, and mutation even though it sits inside
  the jail. This is a path guard, **not** a `*.env` blanket: repo `.env` files
  under `repos/` stay fully editable — copying projects across boxes needs
  them. Uploaded filenames are reduced to a single safe component
  (`os.path.basename`, no separators/traversal).

The jail root is `$GRAVE_ROOT` by design: broad enough to manage repos and
config, with the secret store carved out. It is not a substitute for the OS
permission model — it runs as your user and can touch anything your user owns
*within that tree*.

## What gaming mode does NOT change

Remote access (tailscaled, sshd), the firewall, and gravedecay stay up in
gaming mode. You can always get back in.
