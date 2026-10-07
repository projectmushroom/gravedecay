# Scheduled agent runs

The Linux single-owner appliance can run saved prompts while you are away.
`raise.sh` installs `gravedecay-agents.service`, which checks persistent job
records every ten seconds. It runs as the recorded appliance owner with that
account's HOME and provider logins. macOS, portable containers and multi-user
workspaces do not support this runner yet.

Create a prompt file, then schedule a run:

```sh
grave agents run triage --repo app --prompt-file ~/prompts/triage.txt
grave agents run nightly --repo app --prompt-file ~/prompts/tests.txt --at 02:00
grave agents run weekly --repo app --prompt-file ~/prompts/deps.txt --on 'Mon 03:00' --agent claude
grave agents run fixci --repo app --prompt-file ~/prompts/ci.txt --check 'npm test' --check 'npm run lint'
grave agents run ui --repo app --prompt-file ~/prompts/ui.txt --check 'npm test' --serve 'vite --host 127.0.0.1 --port $PORT --strictPort'
grave agents jobs
grave agents jobs --json
grave agents jobs cancel nightly
```

Without a time flag, a job runs once on the next scheduler scan. `--at HH:MM`
is daily; `--on 'Mon HH:MM'` is weekly (Mon through Sun). Times use the
appliance's local timezone. Job names contain 1–20 letters, digits, dashes or
underscores. Existing names are refused so a new command cannot overwrite
an existing schedule. `--dir /srv/dev/repos/app` is an alternative to `--repo`;
when neither is given, your current directory must be a primary managed repo.

The prompt is copied at creation, so editing or removing the source file does
not change an existing job. Definitions, snapshots and run records live under
`$GRAVE_ROOT/config/secrets/agent-jobs/<name>/`, with private directories and
0600 files; `--check` commands are stored in the job definition with the same
protection. Prompts travel to the provider on stdin, not through a shell or
process arguments. Nothing runs as root. Authenticate the provider as the
appliance owner before scheduling work.

Each run gets a new `agent/job-…` Git branch and an isolated worktree starting
from the source checkout's committed HEAD; that commit is recorded as the run's
`base`. Uncommitted source changes are excluded. Headless output goes to the
existing private `agents/<session>/session-YYYYMMDD.log` archive. The agent
process is supervised by the scheduler; opening its worktree in the terminal
creates an ordinary review shell. `grave agents prune` retains worktrees while
their job is active, then applies its normal dirty/unmerged/unpushed safeguards.

## Provider command and sandbox

Every agent launch on the appliance, scheduled jobs included, builds its
command line from one table, `scripts/providers.py` (`libexec/providers.py`
in the repository). Issue dispatch and the Gravekeeper use the same table, so
the sandbox policy is written once: no launch path adds a permission-bypass
flag; Claude runs `claude -p` under the owner's own permission rules; Codex
runs `codex exec` sandboxed to its working directory (`workspace-write`, the
same mode as dispatch) with approvals set to never, while the Gravekeeper's
row is read-only. A tool that needs additional permission can fail rather than
pause for a human. The runner asks the agent to follow repository
instructions, verify its work, and avoid merging or deploying.

The owner's rules are therefore the night shift's rules, and a fresh login
has none: Claude denies every edit and every shell command it would normally
ask about, and Codex's `workspace-write` sandbox has no network and treats the
worktree's git directory (under the source repository's `.git`) as read-only,
so it can install nothing and commit nothing. Both end `no-changes`. Grant
what a night needs once, as the owner. For Claude, in `~/.claude/settings.json`:

```json
{"permissions": {"defaultMode": "acceptEdits",
  "allow": ["Bash(npm *)", "Bash(npx *)", "Bash(node *)", "Bash(git *)",
            "Bash(python3 *)", "Bash(pytest *)", "Bash(make *)"],
  "deny": ["Bash(git push*)"]}}
```

For Codex, in `~/.codex/config.toml`, network access for installs:

```toml
[sandbox_workspace_write]
network_access = true
```

Codex still cannot commit: its sandbox keeps a worktree's git metadata
read-only whatever `writable_roots` says. Its repair stays as uncommitted
edits in the worktree, which the verdict counts as changes and the checks
run against; the owner reviews and commits. Claude commits as usual.

Checks and the serve command run with the worktree's `.venv/bin` and
`node_modules/.bin` first on PATH, so a Python agent creates its virtualenv at
`.venv` inside the worktree and installs the test tools there; nothing needs
to be installed on the owner's PATH.

## Result package

Provider exit zero means the CLI completed; it is not a result anyone can act
on. After the provider exits, the runner inspects the worktree, runs checks
with a command the agent did not choose, estimates spend and stores the
outcome in the run record under `result`:

```json
"result": {
  "verdict": "ready-for-review",
  "changes": {"commits": 2, "files": 3, "insertions": 41, "deletions": 7, "dirty": false},
  "checks": [{"cmd": "vitest run", "exit_code": 0, "seconds": 38, "tail": "…", "source": "base:3f9c…"}],
  "cost": {"usd": 0.42, "estimated": true, "model": "claude-sonnet-4-5"},
  "quota": {"provider": "codex", "account_wide": true, "plan": "plus",
            "windows": [{"window_minutes": 10080, "used_percent": 46.0, "resets_at": 1790757683}]}
}
```

The lifecycle `status` (`succeeded`, `failed`, `skipped`, `cancelled`,
`timed-out`, `interrupted`) still says how the provider process ended. The
verdict says whether there is something to review:

| verdict | meaning |
| --- | --- |
| `ready-for-review` | provider exited 0, the worktree has commits or uncommitted edits, at least one check ran and every check passed |
| `no-changes` | provider exited 0 and left no commits and a clean tree; checks are skipped |
| `checks-failed` | at least one check exited non-zero (or was stopped) |
| `unverified` | nothing checked the work: no check ran, or the result is missing or malformed; the notification says "No checks ran" |
| `provider-failed` | the provider exited non-zero, or the runner could not launch it |
| `timed-out`, `cancelled`, `interrupted`, `skipped` | the run ended the way `status` says, before a result could be judged |

`changes` comes from `git rev-list --count` and `git diff --shortstat` between
the run's base and HEAD, plus `git status --porcelain` for `dirty`.

`checks` are the commands given with `--check '<cmd>'` (repeatable, up to
eight, each run through `sh -c`), recorded with `source: "owner"`. With no
flag the runner detects one from the run's **base commit**, read with
`git show <base>:…` from the source checkout, never from the worktree, and
records `source: "base:<sha>"`: a `package.json` whose `scripts.test` is not
npm's "no test specified" placeholder runs that script string directly (not
`npm test`, so no `pretest` hook) with the worktree's `.venv/bin` and
`node_modules/.bin` first on `PATH`; a `pyproject.toml` or `pytest.ini` runs `pytest`; a
`Makefile` with a `test` target runs `make test`; otherwise no check runs and
the verdict is `unverified`. A test entry point the agent adds is not
detected; one it rewrites is ignored. `GRAVE_BASE` carries the base SHA to
every check, for owner commands that want to compare against it.
To make the same rule a required status check on the GitHub repository, so
nobody merges around it, see [REQUIRED-CHECKS.md](REQUIRED-CHECKS.md).

Checks run after the provider, in the worktree (uncommitted work and
installed dependencies included), with the same working directory, process
supervision and runtime budget as the provider. They are not run when the
provider failed or made no changes. The command is the owner's or the
base's, but what it executes is the worktree: agent-written tests, fixtures,
`pytest` configuration and Makefile recipes all run, as the owner. Checks are
plain `/bin/sh -c` processes outside the Codex sandbox that confined the
provider; the agent and the runner share one Unix account. See
[SECURITY.md](SECURITY.md). Each check's full output is appended to the
session transcript; the record keeps the command and a tail of its output (up
to 4000 bytes, trimmed so all commands and tails together fit 24 KiB of JSON,
which keeps every run record under the 48 KiB doctor limit; a record that
grows past it is skipped with a warning by the scheduler, listing and
dashboard until doctor is run). A check still running when the job's
timeout, a cancellation or gaming mode ends the run is terminated, recorded
with a non-zero exit and a note in its tail, and the run's `status` reflects
the stop as it would during the provider phase.

`cost` reuses the dashboard's usage parser over the owner's Claude Code
transcripts and Codex rollouts, keeping only sessions whose working directory
was this run's worktree. Prices come from the dashboard's table; a model the
table does not know, every Codex model included, bills at the most expensive
known price so the estimate errs high. `estimated` is always true. `cost` is
null when no transcript for the worktree was found.

`quota` is optional: when the run's own Codex rollout (`codex exec` under
`~/.codex/sessions`, which carries `rate_limits` events) reports the plan
quota, the newest entry with `limit_id: "codex"` and a non-null `primary`
window is stored, one object per window labelled by its `window_minutes`
(300 is the 5-hour window, 10080 the week); the `primary`/`secondary` slot
names never appear, because a weekly-only plan reports the week as `primary`.
Entries for other limits (a trailing `premium` entry has no windows) are
ignored. It is an **account-wide snapshot at the end of the run**, the
position of the ChatGPT-plan sign-in the box uses, not this run's share, and
it exists only for ChatGPT-plan sign-in; an API-key sign-in, a Claude run or
a run whose rollout was not found carries no `quota`. The card shows it as
one line labelled that way. Claude has no documented headless quota source:
the runner never reads Anthropic's OAuth usage endpoint or the Claude
credential file, and a unit test fails on any reference to either.

`test_changes` is an optional, informational list of short rows computed from
the committed work (`base..HEAD` in the worktree) whenever the run made a
commit: deleted or renamed test files, lines removed from existing tests,
added skip/only/xfail markers, a changed `package.json` test script or test
runner config, and touched CI, lockfile or snapshot paths. It is capped at 20
rows of 160 JSON bytes, its size comes out of the 24 KiB check budget, and it
never affects the verdict: the dashboard shows it in amber on the run's card
and the owner decides. Doctor rejects a record whose rows do not fit that
shape. If GitHub ships native test-integrity checks for agent PRs, this list
is the first thing to delete.

`grave agents jobs` shows each job's last status and verdict; `--json` adds
`last_verdict`. A run record without a valid result (written before results
existed, or malformed) receives one when the scheduler next reconciles it: a
still-present worktree can show `no-changes`, otherwise the run is
`unverified`. A record that already carries a valid verdict is never
rewritten, and a record from an older runner (no `source` on its checks)
stays valid.

## Live preview

`--serve '<command using $PORT>'` gives the job one fixed port from
`PREVIEW_RANGE` (grave.conf; never a `PREVIEW_RESERVED` platform port, never
another job's), stored in `job.json` as `serve: {cmd, port}` and shown by
`grave agents jobs`. After a run ends `ready-for-review`, the runner starts the
command in that run's worktree, in its own session, with `PORT` set and the
worktree's `.venv/bin` and `node_modules/.bin` on `PATH`; its output is
appended to the session transcript. The command must listen on **127.0.0.1 only** (read from
`/proc/net/tcp`; a server on `0.0.0.0`, `::` or `::1` is killed and nothing is
mapped), then the port is mapped with `tailscale serve --https=<port>` and
probed at `https://<box>.ts.net:<port>/` through the tailnet name, not raw
loopback, so a dev server that rejects the `ts.net` host (Vite's
`allowedHosts`) fails the check. One HTTP 200 within two minutes records
`preview: {port, url, started}` in the run's `result` and the card shows an
**Open live** button; the push notification keeps its dashboard link. No
answer, a wrong bind or a mapping failure yields no preview and no mapping, the
reason goes to the service journal, and the run still ends
`ready-for-review`.

The preview stops, its mapping is removed and the record gains
`preview.stopped` when: the next run of the same job starts, `grave agents jobs
cancel` removes the job, gaming mode is entered (within the scheduler's
ten-second scan), or 24 hours have passed since `started`. The scheduler
restarts every preview the latest records call live when the service starts
(the serve process dies with the service), keeping the original expiry; a
preview whose process exits on its own is stopped, not restarted. One live
preview per job: a new run replaces the previous one on the same port. It is
the head of the agent's branch only; there is no database clone, no
screenshot, and no base-vs-head twin. `grave preview <port>` refuses a job's
serve port.

The serve command runs agent-written code as the appliance owner, reachable
from every device on the tailnet, for up to a day after the run; see
[SECURITY.md](SECURITY.md). Vite and Next.js each need the bind flag in the
command (`vite --host 127.0.0.1 --port $PORT --strictPort`,
`next dev -H 127.0.0.1 -p $PORT`); Vite also needs the box's tailnet name in
`server.allowedHosts`, see [PORTS.md](PORTS.md).

## Limits and modes

Every job has a two-hour runtime limit; use `--timeout <seconds>` (60–86400) to
change it. The limit covers the provider and its checks together. Two
instances of the same job cannot overlap. Different jobs can run concurrently,
each in its own worktree. Cancellation disables future runs and terminates
current work; use `grave agents jobs cancel <name>` or the report's Cancel job
button. Killing a terminal review shell does not cancel a headless job.
Results and worktrees are retained after cancellation.

A Claude run whose last request was refused with a usage-limit 429 is tried
again when the limit resets. Claude Code writes the refusal to the transcript
(an assistant message flagged `isApiErrorMessage` with `error: "rate_limit"`
and the `quotaLimits.resetsAt` it read from the response headers); after the
provider exits the runner reads that reset time, notes it in the record's
`reason`, and when it is in the future, within eight days and sooner than
the job's next scheduled occurrence (or the job has none), sets the job's
`next_due` to it and records `deferred_to`. The run itself keeps its
`provider-failed` verdict. A reset already in the past, a session that
carried on after the refusal, or a Codex run defers nothing.

Gaming mode is the agent freezer (`/sys/fs/cgroup/grave-torpor/cgroup.freeze`)
and nothing else: the runner does not consult T3, so a job runs to a verdict
with `t3code` stopped or masked, and `gravedecay-agents.service` neither orders
after nor depends on it. A run due while gaming keeps its due time and starts
after the thaw; nothing is recorded. Entering gaming mode during a run cancels
that run within the next mode check (about two seconds, plus process shutdown)
and queues it again once: the cancelled record carries `requeued: <run id>`,
the job's `next_due` becomes now, and the replacement record carries
`requeue_of`; if gaming mode cancels the replacement too, that is final.
The runner never starts services or thaws the appliance. Missed schedule times
while the host was off coalesce into one run at restart. A run interrupted by
a service or host failure is reported as interrupted; it is not automatically
retried. Recurring jobs keep their next scheduled occurrence. DST transitions
do not replay an already claimed occurrence. Records with status `skipped`
come from runners before this behaviour and stay valid.

## Report, notifications and doctor

The dashboard Work tab's **Overnight report** shows one card for each of the
latest 20 runs: verdict, change summary, every check with its source and exit
code, the amber test-change line, cost, the account-wide quota line when the
run recorded one, output tail, full transcript, the
committed diff against the base (`/api/run-diff`, owner-gated, plain text,
256 KiB cap, no caching) and a link to open the worktree; the report API
carries each run's `result`. **Needs attention** counts runs whose verdict is
`checks-failed`, `provider-failed` or `unverified` ([DASHBOARD.md](DASHBOARD.md)).
Reports, the diff and cancellation are owner-gated. Saved prompt files are not
exposed by the report API; provider output can include task text.
Completion, failure and cancellation use the existing `agent-done` notification event;
its deep link stays on the dashboard origin.
Enable a channel in dashboard settings or follow [NOTIFICATIONS.md](NOTIFICATIONS.md).

`grave doctor` checks saved prompt ownership, permissions, schema, source repo,
run consistency, a 48 KiB size limit per run record, that every run gaming
mode cancelled was requeued, and the scheduler service when jobs exist
(including that it does not order after or depend on `t3code.service`), and
separately that every finished run record carries a
valid result and verdict (including a well-formed `test_changes` list when
present, a `quota` snapshot whose every window has a positive
`window_minutes` and reset time, and a `preview` with port, `https://` URL
and times), that every check's `source` is `owner` or
the record's own base commit, that the job command table adds no bypass flag, and that the
installed dashboard carries the review card, its diff route and the live
button. A third line covers previews: every job's serve port is unique,
inside `PREVIEW_RANGE` and not reserved; a `tailscale serve` mapping on a job
port exists only while that job's latest record shows a live preview; every
live preview listener is bound to 127.0.0.1; every other mapping in the range
is one `grave preview` registered in `$GRAVE_ROOT/config/previews`, and every
registration is still mapped.
Restarting the service reconciles unfinished
or verdict-less records left by an interrupted or older worker. Re-raise to
install or update the runner; no individual root-owned timer files need to be
edited. Uninstall removes the scheduler service but retains prompts and
results with the rest of `GRAVE_ROOT`; re-raising resumes retained schedules.
