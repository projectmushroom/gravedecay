# Secrets & MCP wiring for agent CLIs

The pattern that gives **both** T3 connectors (Claude Code and Codex) the same
integration credentials, headlessly, with the key stored in exactly one file.

## The pattern

1. **One env file per integration**, mode 600:

   ```sh
   printf 'LINEAR_API_KEY=lin_api_...\n' > $GRAVE_ROOT/config/secrets/linear.env
   chmod 600 $GRAVE_ROOT/config/secrets/linear.env
   ```

2. **Feed it to T3-spawned sessions.** The shipped t3code.service already
   loads `$GRAVE_ROOT/config/secrets/t3.env`; either put vars there, or add a
   drop-in per integration:

   ```ini
   # /etc/systemd/system/t3code.service.d/linear.conf
   [Service]
   EnvironmentFile=-/srv/dev/config/secrets/linear.env
   ```

   Then `sudo systemctl daemon-reload && sudo systemctl restart t3code`.

3. **Feed it to interactive/tmux shells** — add an env-file loader to your
   shell config (fish example):

   ```fish
   # ~/.config/fish/conf.d/gravedecay-secrets.fish
   for envfile in /srv/dev/config/secrets/*.env
       test -r $envfile; and for line in (string match -rv '^\s*(#|$)' < $envfile)
           set -gx (string split -m1 = $line)
       end
   end
   ```

4. **Register the MCP server in both CLIs**, referencing the env var so the
   key is never copied into their config files:

   ```sh
   claude mcp add --transport http --scope user linear https://mcp.linear.app/mcp \
     --header 'Authorization: Bearer ${LINEAR_API_KEY}'
   codex mcp add linear --url https://mcp.linear.app/mcp \
     --bearer-token-env-var LINEAR_API_KEY
   ```

5. **Verify with a live call from each CLI** (don't trust "configured"):

   ```sh
   claude mcp list                 # expect: linear ... ✓ Connected
   codex exec --skip-git-repo-check \
     'Use the linear MCP get_user tool; reply with just the name.'
   ```

## Why API keys, not OAuth

The box is headless: MCP OAuth flows want a browser with a localhost callback
*on the box*. Most serious MCP providers (Linear, GitHub, Sentry, …) accept
`Authorization: Bearer <api key>` — prefer that. Rotate by editing the one
env file and restarting t3code.

## Multi-user workspaces

Never reuse the appliance owner's integration environment in a developer
unit. Use `grave integrations linear-set <workspace>` and paste the key on
stdin; it writes only that workspace's `config/secrets/linear.env` (600,
owned by its Unix user), registers Linear in that user's Claude and Codex
configuration using `${LINEAR_API_KEY}`, and restarts only that workspace.
`grave integrations status <workspace>` reports configured/onboarding without
printing a value. `linear-logout` deletes the secret and MCP entries.

GitHub authentication likewise runs with the workspace HOME: `github-login`,
`github-logout --user <login>`, and ordinary `gh auth status` can never fall
back to the appliance owner's `~/.config/gh`. Per-workspace dashboard and T3
processes provide cache isolation through separate processes, HOME/XDG paths,
and integration environments.

## Shared coding provider

Multi-user v1 supports administrator-managed API-key authentication only:
`OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, or both. Pipe the assignments to
`grave provider set`; the only credential copy is the root-owned mode-600
`$GRAVE_ROOT/config/secrets/provider.env`. An entitled workspace gets a
root-created reference under `config/workspace-services/` consumed by systemd
before it drops to `grave-<slug>`. That directory is mode 700 and not writable
by workspace users, so a revoked reference cannot be recreated by its user.
Use `grave provider grant|revoke <workspace>` to change entitlement. Revocation
removes the reference and restarts that T3 unit, terminating existing provider
processes so a new session cannot inherit the key.

This is administrative sharing, not credential confidentiality from an
entitled developer. Agent processes receive a directly usable API key in their
environment; a developer who controls those processes can extract and reuse
it. The key is absent from registry/API/status output, generated unit text,
process arguments, normal logs, and default backups. A future compatible local
provider proxy with per-workspace tokens would strengthen this boundary.
Every T3/provider process receives `GRAVE_WORKSPACE_ID`; systemd journal unit
names also contain the workspace slug. These attribute sessions operationally.
Provider-side usage attribution is available only when that provider/CLI
supports forwarding custom metadata; the supported v1 CLIs currently do not.
