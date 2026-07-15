# AGENTS.md — how to raise this box

You are a coding agent asked to turn the machine you are running on into a
gravedecay appliance. Work top to bottom; every step is verifiable. Prefer
running `./raise.sh` and fixing what breaks over doing steps by hand.

## 0. Recon (read-only)

- `cat /etc/os-release` — distro family decides the package manager.
  Arch-family (Arch, CachyOS, EndeavourOS) is first-class; Debian/Ubuntu and
  Fedora are best-effort. Immutable OSes (SteamOS in read-only mode,
  Silverblue) need layering/overlay — stop and report options if you find one.
- `findmnt -no FSTYPE /` — if btrfs, subvolume + snapper setup is worth doing
  (see step 5); if ext4/xfs, skip snapshot steps, everything else works.
- `systemctl is-system-running`, free RAM, disk space. Note anything odd.
- Check for an existing `$GRAVE_ROOT` (default `/srv/dev`) — if present this
  is an adoption, not a fresh raise; diff before overwriting configs.

## 1. Run the ritual

```sh
./raise.sh --profile <generic|t2-macbook|steam-machine>
```

It is idempotent — rerun after fixing any failure. Typical distro quirks you
are expected to solve yourself: package name differences (`docker` vs
`docker.io`), missing `ufw` on Fedora (use firewalld: default-deny, allow ssh
+ tailscale0), nodejs too old on Debian (use NodeSource).

## 2. The two interactive steps (need the human)

1. **Tailscale**: if `tailscale status` shows logged out, run
   `sudo tailscale up --ssh` and give the human the printed auth URL.
   After login, rerun `raise.sh` (it will run `tailscale serve` for both UIs).
2. **T3 pairing**: easiest via the dashboard — ⚙️ settings → "🔑 New T3
   pairing token" streams a token + ready `/pair#token=…` link. CLI
   equivalent: `t3 auth pairing create --base-dir $GRAVE_ROOT/agents/t3code`.
   The human opens the link on the device to enroll it. Agent provider logins (Claude/OpenAI) happen inside the T3 UI or
   via `claude` / `codex login` in a `grave agents new setup` tmux session.

## 3. Verify

- `grave doctor` must pass every check that applies to this host profile.
- `curl -sf http://127.0.0.1:4711/` and `:4712/healthz` return 200.
- `tailscale serve status` shows one origin: `/` → 4711, `/grave` → 4712,
  `/term` → 4713.
- From another tailnet device: T3 loads at `/`, gravedecay at `/grave/` (the
  entry point — the human installs the PWA from there); gravedecay's app tiles
  reach T3 and T3's corner pill (PWA-only) returns to `/grave/`.
- `sudo ufw status` (or firewalld equivalent): default deny incoming, ssh +
  tailscale0 allowed, nothing else.
- `docker ps` — core-postgres and core-redis healthy, ports on 127.0.0.1 only.
- Reboot test if the human allows: everything comes back without login.

## 4. Secrets & MCP (optional, per integration)

Optional but high-value: notifications — an ntfy topic in
`$GRAVE_ROOT/config/secrets/notify.env` makes agents, failing units, and
doctor page the human's phone (`docs/NOTIFICATIONS.md`; doctor verifies the
channel once configured).

Follow `docs/SECRETS.md`. Pattern: key in
`$GRAVE_ROOT/config/secrets/<name>.env` (600), systemd drop-in
`EnvironmentFile` on t3code.service, register the MCP server in **both**
CLIs (`claude mcp add … --header 'Authorization: Bearer ${VAR}'` and
`codex mcp add … --bearer-token-env-var VAR`), restart t3code, verify with a
live tool call from each CLI. Prefer API-key/bearer auth over OAuth — this
box is headless.

For opt-in multi-user mode, never place a collaborator credential in the
appliance owner HOME or shared secret store. Use `grave integrations` and
`grave projects` so commands execute as the workspace Unix identity. Shared
LLM API keys are the sole exception and require explicit `grave provider`
entitlement. `grave users status` and `grave doctor` must agree before and
after user, grant, integration, or provider changes.

## 5. Btrfs niceties (only if / is btrfs)

- `$GRAVE_ROOT` on its own subvolume (e.g. `@srv`) with a snapper config
  (hourly timeline).
- `/var/lib/docker` on a dedicated subvolume (e.g. `@docker`) that snapper
  **never** touches — container churn ruins snapshots.
- Then set `CHECK_DOCKER_SUBVOL=1` and `CHECK_SNAPPER=1` in
  `/etc/gravedecay/grave.conf` so doctor enforces it.

## 6. If the hardware has quirks

Found something machine-specific (crashes on suspend, fan control, GPU
resets)? Mitigate it, then capture it as `profiles/<host>.sh` with a
`profile_apply()` function and a comment explaining *why*, set the matching
`CHECK_*` flags in grave.conf, and add a doctor-verifiable invariant. A quirk
that doctor can't detect will silently regress.

## House rules

- Never bind a service to anything but `127.0.0.1` or the tailnet.
- To let the human see a dev server you started (Vite/Next/etc. on `:3000`
  and friends), keep it bound to `127.0.0.1` and run `grave preview <port>` —
  it exposes the port at `https://<box>.ts.net:<port>` over the tailnet and
  prints the URL. Never bind the dev server to `0.0.0.0` to make it reachable.
- Every listening port gets a row in `docs/PORTS.md` in the same commit.
- Secrets never enter git; `.gitignore` already covers `secrets/` and `.env`.
- Config lives in files under `$GRAVE_ROOT/config` or `/etc/gravedecay` —
  never patch installed binaries or vendor dirs without a reapply script.
- When you change platform behavior, update the matching doc in `docs/` and
  add/adjust a `grave doctor` check. Doctor is the contract.
