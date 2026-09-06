# Security model

## Threat model

A personal box on a home LAN, reachable only over a personal tailnet. The
goal: LAN compromise or a stray port-forward exposes nothing; a lost laptop
or phone can be evicted from the tailnet centrally.

When opt-in multi-user mode is enabled, the identity, role, request-flow, and
failure contract is defined in [MULTIUSER.md](MULTIUSER.md). Each collaborator
runs as a distinct unprivileged Unix user. This protects ordinary credentials,
worktrees, and sessions from other collaborators, but v1 remains for trusted
people—not mutually hostile or public tenants.

## Rules

1. **Nothing listens beyond loopback.** Every service binds `127.0.0.1`.
   Docker containers publish to `127.0.0.1:` only — `grave doctor` fails if
   any container publishes on `0.0.0.0`.
2. **Tailscale is the front door.** `tailscale serve` terminates HTTPS on the
   tailnet and proxies to loopback. Identity comes from the tailnet — serve
   injects `Tailscale-User-Login`, which gravedecay checks before allowing
   action buttons (`GRAVEDECAY_ALLOWED_USERS`).
   In multi-user mode, the LocalAPI socket is mode 660 for root and the
   appliance owner's primary group. Workspace users must not read Serve
   configuration, because its random backend path is the gateway's local
   anti-spoofing capability. `grave doctor` enforces the socket mode.
   A separate root-owned nftables `inet gravedecay` output chain rejects every
   non-root socket owner dialing TCP ports 4711–4713 or 4810–5109. This is
   required in multi-user mode: it protects T3 and ttyd, which cannot validate
   an HTTP header themselves, and is atomically reapplied by a required
   systemd boundary unit. Dashboard backends also require a random gateway
   capability header (except `/healthz`).
3. **SSH**: key-only (`PasswordAuthentication no` — doctor-enforced),
   plus Tailscale SSH as a fallback door. Note: Tailscale SSH intercepts
   port 22 *over the tailnet*; plain sshd remains reachable via LAN IPs only.
4. **Firewall default-deny incoming**; allow ssh + the `tailscale0`
   interface. `raise.sh` allows SSH *before* enabling so you're not locked out.
5. **Secrets** live in `$GRAVE_ROOT/config/secrets/*.env`, mode 600,
   git-ignored, delivered via systemd `EnvironmentFile=` — never in unit
   files, shell configs, or agent JSON configs (use `${VAR}` expansion).
   The optional `t3-activity.env` contains only a short-lived
   `orchestration:read` bearer plus a local-loopback or tailnet-HTTPS source;
   it must never receive operate, terminal, admin, relay, or provider scope.
   Grave polls only T3's shell projection, does not read T3 SQLite or request
   transcripts/details, and keeps it behind the owner/workspace dashboard
   boundary. Doctor probes it only when all four activity settings exist.
6. **macOS companion updates** run as the logged-in user through a fixed
   LaunchAgent/helper path. Validated semantic tags/channels, a fixed trusted
   origin, argv execution, root-confined staging, and rollback prevent an
   update request from becoming an arbitrary command. It has no privileged
   operation and does not alter the Tailscale connection or host services.
7. **Native macOS advertising is opt-in and loopback-only.** The native app
   can serve only `/healthz` and `/api/v1/summary` from `127.0.0.1:4712` while
   its normal app process runs. It refuses an existing legacy companion or
   occupied port, and never changes Tailscale Serve automatically; publishing
   `/grave` is a visible manual command because it cannot safely prove global
   Serve-route ownership.

## T3 Connect (the one sanctioned exception to tailnet-only)

T3 Code's official apps (iOS/Android/desktop) can reach the box through
upstream's managed relay instead of the tailnet. This is **off by default**
and opt-in per box via `grave t3 connect <mode>`:

- **`publish`** keeps the tailnet as the only transport and only pushes agent
  *activity* (notifications, Live Activities) to your signed-in apps — no
  tunnel, no inbound path. This is the recommended mode.
- **`full`** lets the appliance's T3 server run a managed outbound tunnel
  (`cloudflared` → `relay.t3.codes`), making it reachable from the official
  apps without Tailscale. Understand the trade before enabling: your agent
  sessions, terminals, and diffs transit T3's relay infrastructure, access is
  gated by their Clerk account auth (scoped capability tokens), and upstream
  does not document end-to-end encryption across the relay — assume the relay
  operator is in the traffic path. Nothing new *listens* (the tunnel is
  outbound-only) and the firewall posture is unchanged, but the trust boundary
  now includes a third party.

The declared mode lives in `config/t3-connect.mode` and **doctor enforces
it**: link state must match the declaration, `publish` must not have a tunnel
process, `off` must have neither. Doctor also warns about Connect identities
outside the appliance instance — a bare `t3` run or the desktop app links the
default `~/.t3` profile, which is a second relay identity this contract does
not cover. All `grave` tooling pins `--base-dir` to the appliance instance
for exactly that reason.

### Freeing a managed-tunnel slot

A saved login is separate from a working environment link. Start with:

```sh
grave t3 connect status
grave t3 connect diagnose
```

Connect commands run as the `t3code.service` Unix user, including when invoked
as root. This keeps login files readable by the service. Doctor also checks
that the service user can read and update the private mode-600 Connect files.
If an earlier root invocation created inaccessible files, correct ownership
of the affected files under the appliance's `userdata/secrets` directory to
the service user, retaining mode 600. Do not recursively change ownership of
other profiles or collaborator credentials.

`diagnose` is read-only: it shows the latest recorded link attempt and calls
`GET https://relay.t3.codes/v1/environments` with the appliance's existing
credential. Tokens stay in memory, never in command arguments or output.
The result identifies each environment by label, id, and transport. Manual
endpoints do not consume Cloudflare tunnel slots. The list does not expose the
account's maximum, so counting entries alone cannot confirm quota exhaustion.
Older T3 versions (including 0.0.38) log HTTP 403 but discard the response body;
diagnostics report that limitation instead of declaring every 403 a quota error.
When preserved in the trace, `environment_link_limit_exceeded` identifies the
quota failure explicitly.

In the official app, open the account menu's **T3 Connect** page, or mobile
**Settings → T3 Connect**, and **Deregister** an unused environment. This revokes
that machine's cloud access and frees its host slot. Then run:

```sh
sudo systemctl restart t3code
grave t3 connect status
grave doctor
```

For administration by API, the removal endpoint is
`DELETE /v1/client/environment-links/:environmentId`. Obtain the id from the
list above and choose it explicitly; never remove an arbitrary environment to
make a health check green. If DELETE returns an upstream error, list again
before retrying: removal can have succeeded despite an HTTP 500. The new host
must actually link and start its tunnel before recovery is complete.

`publish` mode consumes no managed tunnel slot, but changing to it requires
Tailscale for transport; it is not a repair for someone expecting full Connect.

See [upstream remote-access documentation](https://github.com/pingdotgg/t3code/blob/main/docs/user/remote-access.md).

## The sudoers file

Fresh installs offer a dedicated `grave` owner, a custom account, or the current
user. This is a trusted appliance administrator, not an isolation boundary.
A dedicated account selected from another administrator gets the validated
password-required rule `/etc/sudoers.d/40-gravedecay-owner` for initial setup
and manual maintenance. Its GitHub/provider credentials stay in its own home;
administrator credentials are never copied. The installer enters the owner's
login context before provisioning. Existing owner changes require a separate
migration. See [INSTALL.md](INSTALL.md).

`raise.sh` installs `/etc/sudoers.d/50-gravedecay`: NOPASSWD for your user on
`systemctl`, `docker`, `grave`, `journalctl`, `ufw`, `snapper`, `sshd -T`, and
the fixed-logic firewall wrappers `/usr/libexec/gravedecay/firewall-harden`
(no-argument default-deny setup) and `firewall-status` (read-only queries for
`grave doctor`) — raw `firewall-cmd` is deliberately never granted, since it
can rewrite any firewall rule. The authorized `grave` root
helper creates the transient gaming auto-thaw timer without granting direct
`systemd-run` access. This is effectively
root-equivalent for *your* user (systemctl alone gets you there); the point is
convenience for a single-human box, not privilege separation. If your box has
other human users, tighten it.

## The web terminal

The following shared-terminal behavior applies only to default single-user
mode. Multi-user mode routes `/term` to a ttyd/tmux instance running as the
caller's dedicated Unix user; the gateway denies unknown/disabled callers and
prevents backend selection. Direct non-root loopback access to both the shared
legacy terminal port and every workspace terminal port is rejected by the
multi-user nftables boundary; legacy T3 and terminal services are disabled on
every multi-user re-raise.

`/term` (ttyd → the shared `tmux -L agents` socket) is an interactive shell as
your user for **anyone who can reach it** — ttyd does not check the
`Tailscale-User-Login` header. On a personal tailnet this is the same trust
you already extend via Tailscale SSH; on a shared tailnet, restrict who can
reach this node with Tailscale ACLs or disable `gravedecay-term`.

### macOS agents layer

`macos/install.sh --agents` extends the same trust trade to a serving Mac,
stated plainly: tailnet identity ⇒ your macOS Unix account. The Mac's ttyd
(`io.gravedecay.term`, `/term`) is a shell as the logged-in Mac user for
anyone who can reach it, exactly like the Linux note above, and T3 sessions
run as that user too. Mitigations, not absolution: agent sessions and T3's
project root live in a dedicated `~/Grave` jail rather than the whole HOME,
every reopened dashboard endpoint (pairing, session kill/copy) stays behind
the exact-`LoginName` `ALLOWED_USERS` gate, binds stay on loopback behind
Tailscale Serve, and nothing runs with sudo. A shell can still leave the
jail — that is what shells do — so on a Mac that is also a personal machine,
do not enable this mode; on a dedicated mini, prefer a separate macOS user
account. Companion secrets (Linear key, ntfy) stay in owner-only files under
the Application Support root, outside the jail.

## Portable Docker workspace

The portable Compose deployment is not a way to containerize the trusted host
appliance. It publishes only nginx on `127.0.0.1`, with app services private to
its Compose network, drops all capabilities, runs long-lived services without
root, and has no Docker socket, privileged mode, host namespaces, systemd, or
in-container Tailscale. The agent HOME volume is outside the dashboard's
`GRAVE_ROOT` file-manager jail, so browser file operations cannot reach CLI
credentials.

Use Docker Engine 28.0.0+ for localhost-published ports. On older Engines,
Docker documented that a loopback published port could be reachable by hosts on
the same L2 segment; upgrade or deny it in the host firewall. Never put the
portable gateway behind an unauthenticated public/LAN proxy: a request without
the Tailscale identity header is treated as local. Use host Tailscale Serve or
another trusted identity-aware access-control proxy instead. See
[DOCKER.md](DOCKER.md) for the operational contract.

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

## Agent "skip-perms" (⚡) tiles

⚙️ settings can flip the Claude/Codex launcher tiles into ⚡ **skip-perms**
mode: they open a `*-yolo` web-terminal session that runs the agent with all
gates off (`claude --dangerously-skip-permissions` /
`codex --dangerously-bypass-approvals-and-sandbox`) — no per-command approval,
no sandbox. The agent can then run anything your user can.

That is only defensible under this box's threat model: a single-human,
tailnet-only appliance where the web terminal is already an un-gated shell as
your user (see above) and the sudoers file is already root-equivalent for you.
It is **off by default**, opt-in per tile, and the toggle is itself
identity-gated like every other setting. Each yolo session is a distinct tmux
session name, so it never shares state with a gated one. If your box has other
human users, or a wider tailnet, leave it off.

## What gaming mode does NOT change

Remote access (tailscaled, sshd), the firewall, and gravedecay stay up in
gaming mode. You can always get back in.

Issue-to-agent dispatch uses the same owner and cross-site gates as dashboard
mutations. It accepts identifiers and allowlisted choices, retrieves issue
content from Linear, and passes it as task data to the selected CLI without
permission-bypass flags. Task files are owner-private and included in agent
worktree backups. Workspace, portable, and macOS backends reject this launch
path; it never crosses into the appliance owner's identity. See
[DISPATCH.md](DISPATCH.md).
