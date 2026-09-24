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

## Result package

Provider exit zero means the CLI completed; it is not a result anyone can act
on. After the provider exits, the runner itself, never the agent, inspects the
worktree, runs checks, estimates spend and stores the outcome in the run
record under `result`:

```json
"result": {
  "verdict": "ready-for-review",
  "changes": {"commits": 2, "files": 3, "insertions": 41, "deletions": 7, "dirty": false},
  "checks": [{"cmd": "npm test", "exit_code": 0, "seconds": 38, "tail": "…"}],
  "cost": {"usd": 0.42, "estimated": true, "model": "claude-sonnet-4-5"}
}
```

The lifecycle `status` (`succeeded`, `failed`, `skipped`, `cancelled`,
`timed-out`, `interrupted`) still says how the provider process ended. The
verdict says whether there is something to review:

| verdict | meaning |
| --- | --- |
| `ready-for-review` | provider exited 0, the worktree has commits or uncommitted edits, every check passed |
| `no-changes` | provider exited 0 and left no commits and a clean tree; checks are skipped |
| `checks-failed` | at least one check exited non-zero (or was stopped) |
| `provider-failed` | the provider exited non-zero, or the runner could not launch it |
| `timed-out`, `cancelled`, `interrupted`, `skipped` | the run ended the way `status` says, before a result could be judged |

`changes` comes from `git rev-list --count` and `git diff --shortstat` between
the run's base and HEAD, plus `git status --porcelain` for `dirty`.

`checks` are the commands given with `--check '<cmd>'` (repeatable, up to
eight, each run through `sh -c`). With no flag the runner detects one in the
worktree: a `package.json` whose `scripts.test` is not npm's "no test
specified" placeholder runs `npm test`; a `pyproject.toml` or `pytest.ini`
runs `pytest`; a `Makefile` with a `test` target runs `make test`; otherwise
no check runs. Checks run after the provider, in the worktree, with the same
working directory, environment, process supervision and runtime budget as the
provider. They are not run when the provider failed or made no changes. Each
check's full output is appended to the session transcript; the record keeps
the last 4000 bytes as `tail`. A check still running when the job's timeout,
a cancellation or gaming mode ends the run is terminated, recorded with a
non-zero exit and a note in its tail, and the run's `status` reflects the
stop as it would during the provider phase.

`cost` reuses the dashboard's usage parser over the owner's Claude Code
transcripts and Codex rollouts, keeping only sessions whose working directory
was this run's worktree. Prices come from the dashboard's table; a model the
table does not know, every Codex model included, bills at the most expensive
known price so the estimate errs high. `estimated` is always true. `cost` is
null when no transcript for the worktree was found.

`grave agents jobs` shows each job's last status and verdict; `--json` adds
`last_verdict`. Run records written before results existed receive a verdict
when the scheduler next reconciles them: a still-present worktree is inspected
for changes, otherwise the run is marked `ready-for-review` so a human decides.

## Limits and modes

Every job has a two-hour runtime limit; use `--timeout <seconds>` (60–86400) to
change it. The limit covers the provider and its checks together. Two
instances of the same job cannot overlap. Different jobs can run concurrently,
each in its own worktree. Cancellation disables future runs and terminates
current work; use `grave agents jobs cancel <name>` or the report's Cancel job
button. Killing a terminal review shell does not cancel a headless job.
Results and worktrees are retained after cancellation.

If developer services are stopped or the agent freezer is active when a run
is due, it is recorded as skipped. Entering gaming mode during a run cancels
that run within the next mode check (about two seconds, plus process shutdown).
The runner never starts services or thaws the appliance. Missed schedule times
while the host was off coalesce into one run at restart, or one recorded skip
if it restarts in gaming mode. A run interrupted by a service or host failure
is reported as interrupted; it is not automatically retried. Recurring jobs
keep their next scheduled occurrence. DST transitions do not replay an already
claimed occurrence.

## Report, notifications and doctor

The dashboard Work tab's **Overnight report** shows the latest 20 runs, exit
status, output tail, full transcript and a link to open the worktree; the
report API carries each run's `result`. Reports and cancellation are
owner-gated. Saved prompt files are not exposed by the report API; provider
output can include task text.
Completion, failure and skips use the existing `agent-done` notification event.
Enable a channel in dashboard settings or follow [NOTIFICATIONS.md](NOTIFICATIONS.md).

`grave doctor` checks saved prompt ownership, permissions, schema, source repo,
run consistency and the scheduler service when jobs exist, and separately that
every finished run record carries a valid result and verdict and that the job
command table adds no bypass flag. Restarting the service reconciles unfinished
or verdict-less records left by an interrupted or older worker. Re-raise to
install or update the runner; no individual root-owned timer files need to be
edited. Uninstall removes the scheduler service but retains prompts and
results with the rest of `GRAVE_ROOT`; re-raising resumes retained schedules.
