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
3. **SSH**: key-only (`PasswordAuthentication no` — doctor-enforced),
   plus Tailscale SSH as a fallback door. Note: Tailscale SSH intercepts
   port 22 *over the tailnet*; plain sshd remains reachable via LAN IPs only.
4. **Firewall default-deny incoming**; allow ssh + the `tailscale0`
   interface. `raise.sh` allows SSH *before* enabling so you're not locked out.
5. **Secrets** live in `$GRAVE_ROOT/config/secrets/*.env`, mode 600,
   git-ignored, delivered via systemd `EnvironmentFile=` — never in unit
   files, shell configs, or agent JSON configs (use `${VAR}` expansion).

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

A `full` link can be refused with `403 POST
https://relay.t3.codes/v1/client/environment-links` while everything local
looks correct — authorized, link desired, relay client installed. Upstream's
contract (`packages/contracts/src/relay.ts`) maps exactly two codes to 403 on
that endpoint: `environment_connect_not_authorized` and
`environment_link_limit_exceeded` ("this account allows at most N tunnels").
T3 Code logs the status but **discards the response body**, so the code never
reaches the journal — the limit is the usual culprit, and the desktop app is
the usual holder (it links the default `~/.t3` profile).

Two facts make this harder than it should be:

- `t3 connect unlink` does attempt the server-side revoke, but **degrades to
  local-only when that profile holds no CLI credential** — which is exactly
  the desktop app's state (`authenticated: false`, `linked: true`). It prints
  "T3 Connect is disabled locally" and the slot stays taken.
- The relay exposes **no list endpoint and no web UI** for environment links —
  only `DELETE /v1/client/environment-links/:environmentId`. There is no page
  to go and tidy this up on.

So release the slot by id. The id and a bearer both survive on disk:

```sh
ENV=$(cat ~/.t3/userdata/environment-id)          # the stray profile's id
TOK=$(jq -r .accessToken \
  "$GRAVE_ROOT/agents/t3code/userdata/secrets/cloud-cli-oauth-token.bin")
curl -fsS -X DELETE -H "Authorization: Bearer $TOK" \
  "https://relay.t3.codes/v1/client/environment-links/$ENV"   # -> {"ok":true}
sudo systemctl restart t3code
grave t3 connect status                            # linked: yes, relay: https://…
```

Any valid bearer for the account authorizes the delete, so the appliance's own
token works on another environment's link. Deleting is reversible — relinking
the desktop app re-registers it, and takes the slot back.

`publish` mode consumes no tunnel slot, so it stays available when the limit
is genuinely full.

## The sudoers file

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
prevents backend selection.

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
