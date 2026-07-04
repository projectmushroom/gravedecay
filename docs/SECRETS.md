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
