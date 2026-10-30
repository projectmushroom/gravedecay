# Diggers

A **digger** is a focused agent bound to one job, one repository and the
channels you bind it to. Someone asks for a repeatable thing ("create a new
white label for project X"); the digger does it inside the rules the
repository ships, on **its own Codex or Claude login**, and gravedecay is
where the owner defines, watches, pauses and removes it. The Gravekeeper
watches the grave and explains; a digger gets its hands dirty.

What is not claimed: a hostile-tenant boundary, that the agent cannot
influence the result, or that a prompt is a guardrail. What is claimed is
narrower: tools the definition allows and nothing else, a sandbox where the
provider has one, and, for the `dedicated` tier, a Unix identity that cannot
read the owner's secrets.

This is milestone 1 of the epic (#243): definitions, identity tiers, the
digger's own provider login, the compiled deny-by-default policy and the doctor
checks. **Nothing runs a turn yet.** The runner and the dashboard channel
arrive in M2 (#246); Slack intake, approvals and budget enforcement in M3
(#247). OpenCode is a later provider (#245).

## Quick start

```sh
# a client-facing digger: its own Unix user, HOME, clone and Codex login
grave digger new wl --repo app --provider codex --identity dedicated \
  --mcp whitelabel='node .grave/mcp/whitelabel.js'
grave digger login wl              # device code; confirm on your phone
grave digger status wl             # {"loggedIn": true, ...}; never prints a token
grave doctor                       # every digger invariant, login as a warning

# a second subscription on the owner's account, no new Unix user
grave digger new wl2 --repo app --provider claude --identity separate-login
grave digger login wl2             # Claude subscription; --console for API billing
```

`grave digger login <name>` runs in whatever terminal you call it from; the
terminal tile opens the same one-shot flow at `/term/?arg=auth-digger-<name>`.
The `auth-` prefix is what makes that session end when the flow ends.

Other verbs: `list`, `show <name>`, `enable <name>` (validate and recompile
after editing the definition), `disable <name>`, `remove <name> [--purge]`.

## The definition

Each digger lives in `$GRAVE_ROOT/config/diggers/<name>/` (0700, the owner's):

| File | What |
| --- | --- |
| `digger.json` (0600) | The definition below; validated on write, on `enable` and by doctor |
| `DIGGER.md` (0600) | Instructions and job knowledge. Not a credential store and not a guardrail |
| `policy/` (0700) | Compiled provider policy for the owner-account tiers; generated, never edited |
| `codex/` or `claude/` (0700) | `separate-login` only: the digger's own `CODEX_HOME` / `CLAUDE_CONFIG_DIR` |

```json
{"name": "whitelabel", "provider": "codex", "model": "",
 "identity": "dedicated",
 "repo": "app", "work": "clone",
 "tools": {"mcp": {"whitelabel": {"command": "node", "args": [".grave/mcp/whitelabel.js"]}},
           "allow": ["mcp__whitelabel__*"], "deny": ["Bash(git push*)"]},
 "budget": {"max_turns": 30, "timeout_s": 1800, "daily_turns": 200, "daily_usd": 10.0},
 "pause_in_gaming": true, "enabled": true}
```

- `name`: `^[a-z][a-z0-9-]{1,19}$`, never reused while the old one is retained;
  `keeper` is reserved.
- `provider`: `codex` or `claude`, from the one provider table.
- `identity`: `shared`, `separate-login` or `dedicated` (below).
- `repo`: a primary, non-symlink checkout under `$GRAVE_ROOT/repos`.
- `work`: `clone` for `dedicated` (the digger cannot share the owner-owned
  object store), `worktree` for the owner-account tiers.
- `tools.mcp`: at most 8 stdio servers, `command` and `args` only. The
  recommended pattern is a server the repository ships doing the one
  validated operation, listed as the digger's only tool.
- `tools.allow` / `tools.deny`: rules in Claude's syntax: a built-in tool
  (`Read`, `Grep`, `Glob`, `Edit`, `Write`, `Bash`, `NotebookEdit`,
  `TodoWrite`, `WebFetch`, `WebSearch`), optionally narrowed (`Bash(npm test)`),
  or an MCP tool (`mcp__<id>__*`, `mcp__<id>__<tool>`) of a server defined above.
  Unknown tools are refused. **An allowlist that is empty after deny is a
  validation error**: a digger with no tools refuses to exist rather than
  running text-only.
- `budget`: recorded now, enforced from M3; `max_turns` is compiled into the
  Claude command already.

Unknown keys anywhere are refused, so a typo cannot silently mean "default".
`grave digger new` writes a starter `DIGGER.md` and, unless you pass
`--allow`, allows the `--mcp` servers you name or else `Read`, `Grep`, `Glob`.
Edit `digger.json` by hand for anything else, then `grave digger enable <name>`.

## Identity tiers

| Tier | Runs as | Provider login | Protects |
| --- | --- | --- | --- |
| `shared` | the owner | the owner's own `~/.codex` / `~/.claude` | Nothing new: today's behaviour with a narrower tool policy |
| `separate-login` | the owner | `config/diggers/<name>/codex` or `…/claude` | The owner's quota and login from the digger's traffic; not the owner's files |
| `dedicated` | `grave-digger-<name>` | that user's own HOME | The owner's files, secrets, `~/.config/gh` and logins from the digger |

Client-facing diggers must be `dedicated`. `new` defaults to it.

**separate-login** points `CODEX_HOME` or `CLAUDE_CONFIG_DIR` at a 0700
directory in the config tree. Both CLIs honour the variable for status and
login; a fresh one reports logged out. Codex refuses a home under a temporary
directory ("Refusing to create helper binaries under temporary dir"), which is
why it lives under `$GRAVE_ROOT/config` and never in a tmpdir.

**dedicated** creates a system user `grave-digger-<name>` (no login shell),
HOME `$GRAVE_ROOT/diggers/<name>` (0700). Root only creates and validates the
account and HOME; drift (a different HOME, uid 0, a group- or world-readable
HOME, a symlink) is reported and never repaired. Everything inside the HOME is
done *as* the digger through `runuser`:

- **Clone**: `$HOME/repos/<repo>` from a bundle of the owner's primary
  checkout's committed `HEAD` (uncommitted work stays home), on the same branch
  name, with `origin` set to the source repo's origin. No identity ever runs
  Git in a repository another identity owns. Pushing needs the digger's own
  GitHub credential (a deploy key, or `gh auth login` as that user); the
  owner's `~/.config/gh` is never reachable. A GitHub login verb arrives with
  the runner, which is the first thing that pushes.
- **Provider CLI**: the owner's CLI usually lives under the owner's 0700 HOME
  (mise, Homebrew, `~/.local`), which the digger must not reach. Provisioning
  copies the owner's native build into a root-owned, read-only
  `$GRAVE_ROOT/diggers/.tools/` shared by every digger (set-id bits dropped,
  scripts refused). `enable` or `login` refreshes it; doctor warns when the
  owner's CLI has moved on. A system-wide install works the same way. The
  build is found through `PATH` and then `mise which`, looking past launcher
  scripts (Omarchy's `~/.local/bin/codex` is one); set `DIGGER_CODEX_BIN` /
  `DIGGER_CLAUDE_BIN` in `grave.conf` when neither finds it.
- **Policy**: a root-owned, digger-readable copy of the definition, `DIGGER.md`
  and the compiled settings in `$GRAVE_ROOT/diggers/.policy/<name>/`
  (0750/0640), so the agent cannot rewrite its own policy mid-run.

The root half (`grave __digger …`) runs through the scoped `sudo -n grave`
grant the owner already has. The owner's administrator rule is `(root) ALL`
with a password, which cannot `sudo -u` another user, and no new NOPASSWD rule
is added. Run `new`, `login`, `status` and `enable` for a dedicated digger from
the terminal tile or SSH; the T3 service runs with `NoNewPrivileges` and cannot
use sudo at all.

## Login

| Provider | Default | Alternative |
| --- | --- | --- |
| Codex | `codex login --device-auth` (prints a URL and a code) | `--api-key`: `codex login --with-api-key`, key read without echo and passed on stdin |
| Claude | `claude auth login --claudeai` (subscription) | `--console`: Anthropic Console, API billing |

`shared` diggers have nothing separate to log in. `status` reads
`codex login status` / `claude auth status` for that identity, with secret-
looking variables removed from the environment so an API key in your shell
cannot make it lie, and prints only `loggedIn`.

**Plan terms are yours to settle.** Consumer subscriptions are sold for
individual use. The clean routes for a digger serving client traffic are a
Business or Team seat per digger, or API-key billing (`codex login
--with-api-key`, `claude auth login --console`).

## The compiled policy

`digger_command()` in `scripts/providers.py` turns the definition into one
command per provider, beside the job, dispatch and Keeper commands. The prompt
always arrives on stdin. No bypass flag is emitted in any spelling, and
`policy_ok` is asserted over the whole command line, MCP arguments and Codex
instructions included.

**Claude**: `claude -p --output-format stream-json --verbose --max-turns <n>
--setting-sources "" --settings policy/claude-settings.json --strict-mcp-config
--mcp-config policy/claude-mcp.json --tools <built-ins> --disallowedTools …
--append-system-prompt-file DIGGER.md`. The settings carry
`defaultMode: dontAsk` (anything not allowed is refused, never prompted), the
allow and deny lists, `disableBypassPermissionsMode` and `disableAllHooks`.
No user, project or local settings file is loaded, so neither the owner's
rules nor a file the repo ships can widen the list; only the generated servers
load; `--tools` removes every built-in not named.

**Codex**: `codex exec --json --ignore-user-config -c sandbox_mode="workspace-write"
-c approval_policy="never" -c web_search="disabled" --disable …` with each
allowed MCP server as `-c mcp_servers.<id>.…` (`enabled_tools` /
`disabled_tools` when the rules name tools) and `DIGGER.md` as
`developer_instructions`. No `config.toml` is read, which is stronger than
generating one, so nothing the owner or a previous login configured can add a
server. Apps, plugins, browser and computer use, image generation, hooks and
sub-agents are always off.

The honest limits:

- **Codex has one shell, behind its sandbox.** With no built-in tool allowed,
  the shell is disabled. Allowing any built-in (`Read` included) enables the
  whole shell inside `workspace-write` (writes only in the work directory, no
  network); a `Bash(...)` allow pattern narrows only on Claude.
- **Codex `Bash(...)` deny rules** compile to execpolicy `forbidden` prefix
  rules in the digger's own `CODEX_HOME/rules/digger.rules`, so they need
  `separate-login` or `dedicated`; a `shared` Codex digger with a shell and a
  `Bash` deny rule is refused. Only literal prefixes are expressible.
- **Claude has no OS sandbox.** Its built-in tools run as the digger's Unix
  identity under the permission rules; for `dedicated` that identity is the
  boundary.
- **DIGGER.md is instructions.** A rule in it is a request, not a guardrail.
  It reaches Codex as one argument, so it is capped at 32 KiB and must not
  spell a bypass flag.

## Doctor

`grave doctor` (single-owner mode) runs `diggers.py check` and reports, per
digger: the definition parses and matches the schema (unknown keys fail), the
repo is a primary non-symlink checkout, the store and files are the owner's
with 0700/0600, the compiled policy on disk matches a fresh compile, the
compiled command passes `policy_ok`, and the provider CLI is present. For
`dedicated` it also checks, as root: the account exists with the recorded
HOME and a non-zero uid, HOME is a real 0700 directory it owns, the clone is a
real checkout the digger owns with the source repo's `origin`, the root-owned
policy and the Codex rules match, the provider is executable by that user, and
that the owner's HOME is closed to other users. It verifies the digger unit
templates are installed and that none is running yet.

A missing login is a **warning**, not a failure: logging in is a post-raise
owner step, like the Keeper's. `grave digger login <name>` clears it.

## Disk cost

A `dedicated` digger's clone is a full copy of the repository's history, not a
worktree: budget the size of `.git` plus a checkout per digger. The provider
copy is shared by every digger and replaced on refresh (Codex about 400 MB,
Claude about 270 MB; on btrfs with reflinks the copy is nearly free).

## Units

`raise.sh` installs `gravedecay-digger@.service` (`User=grave-digger-%i`,
`ProtectHome=yes`, writes only to the digger's HOME) and
`gravedecay-digger-owner@.service` (`User=` the owner, for the owner-account
tiers). Both run `diggers.py run <name>` with `NoNewPrivileges`,
`ProtectSystem=strict`, a private `/tmp` and the workspace CPU and memory
ceilings. Neither has an `[Install]` section and neither is started in this
milestone. A digger never listens on a port: intake is outbound only, so
`docs/PORTS.md` is unchanged.

## Removing

`grave digger remove <name>` moves the definition (and a `separate-login`
login) to `config/diggers/.removed/<name>-<time>/`, removes the root-owned
policy and disables the unit. A `dedicated` digger's Unix user, HOME, clone
and login are kept; defining the same name again adopts them. `--purge`
deletes all of it, the Unix user included. `grave uninstall` lists remaining
`grave-digger-*` users rather than deleting them.
