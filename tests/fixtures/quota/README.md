# Quota fixtures

Transcript shapes used by `tests/test_quota_snapshot.py` and the runner tests.
`__CWD__` is replaced with the worktree path at test time.

- `rollout-weekly-only.jsonl`, `rollout-5h-plus-weekly.jsonl`,
  `rollout-trailing-premium.jsonl`: `codex exec` rollouts as written by
  codex-cli 0.160.0 on the appliance (the weekly-only shape is a verbatim
  observation: a ChatGPT-plan sign-in reports the week as `primary` with
  `secondary` null, so windows are labelled by `window_minutes`, never by slot;
  a trailing `premium` entry has no windows and must not replace the codex one).
- `claude-429.jsonl`: a Claude Code transcript whose last request was refused
  with a usage-limit 429, in the shape Claude Code 2.1.289 writes
  (`isApiErrorMessage`, `error: "rate_limit"`, `quotaLimits.resetsAt`). No
  real 429 had been recorded on the box when this fixture was written.
