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
0600 files. Prompts travel to the provider on stdin, not through a shell or
process arguments. Nothing runs as root. Authenticate the provider as the
appliance owner before scheduling work.

Each run gets a new `agent/job-…` Git branch and an isolated worktree starting
from the source checkout's committed HEAD. Uncommitted source changes are
excluded. Headless output goes to the existing private
`agents/<session>/session-YYYYMMDD.log` archive. The agent process is supervised
by the scheduler; opening its worktree in the terminal creates an ordinary
review shell. `grave agents prune` retains worktrees while their job is active,
then applies its normal dirty/unmerged/unpushed safeguards.

Codex runs with `codex exec --sandbox workspace-write`; Claude uses `claude -p`
with the owner's configured permissions. No permission-bypass flags are added.
A tool that needs additional permission can fail rather than pause for a human.
The runner asks the agent to follow repository instructions, verify its work,
and avoid merging or deploying. Provider exit zero means the CLI completed;
review the report and changes before treating the requested task as successful.

Every job has a two-hour runtime limit; use `--timeout <seconds>` (60–86400) to
change it. Two instances of the same job cannot overlap. Different jobs can
run concurrently, each in its own worktree. Cancellation disables future runs
and terminates current work; use `grave agents jobs cancel <name>` or the
report's Cancel job button. Killing a terminal review shell does not cancel
a headless job. Results and worktrees are retained after cancellation.

If developer services are stopped or the agent freezer is active when a run
is due, it is recorded as skipped. Entering gaming mode during a run cancels
that run within the next mode check (about two seconds, plus process shutdown).
The runner never starts services or thaws the appliance. Missed schedule times
while the host was off coalesce into one run at restart, or one recorded skip
if it restarts in gaming mode. A run interrupted by a service or host failure
is reported as interrupted; it is not automatically retried. Recurring jobs
keep their next scheduled occurrence. DST transitions do not replay an already
claimed occurrence.

The dashboard Work tab's **Overnight report** shows the latest 20 runs, exit
status, output tail, full transcript and a link to open the worktree. Reports
and cancellation are owner-gated. Saved prompt files are not exposed by the
report API; provider output can include task text.
Completion, failure and skips use the existing `agent-done` notification event.
Enable a channel in dashboard settings or follow [NOTIFICATIONS.md](NOTIFICATIONS.md).

`grave doctor` checks saved prompt ownership, permissions, schema, source repo,
run consistency and the scheduler service when jobs exist. Restarting the
service reconciles unfinished records left by an interrupted worker. Re-raise
to install or update the runner; no individual root-owned timer files need to
be edited. Uninstall removes the scheduler service but retains prompts and
results with the rest of `GRAVE_ROOT`; re-raising resumes retained schedules.
