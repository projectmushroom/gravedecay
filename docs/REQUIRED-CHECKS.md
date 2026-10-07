# Required checks: a test-integrity gate the merge button obeys

The runner's amber **Test changes** line is informational. If you want deleted
or weakened tests to block a merge, put a test-integrity workflow on the
repository and make it a required status check through a GitHub ruleset. Two
free tools do the job; both read the pull request diff and fail when test
files were deleted, skip/only/xfail markers were added, or assertions were
removed without an explicit override.

## 1. Add the workflow

[gatekeep](https://github.com/SagnikKK1/gatekeep) (Apache-2.0):

```yaml
# .github/workflows/gatekeep.yml
name: gatekeep
on: { pull_request: {} }
permissions: { contents: read, checks: write }
jobs:
  gatekeep:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with: { fetch-depth: 0 }
      - uses: SagnikKK1/gatekeep@v1
```

[discipline](https://github.com/orieg/discipline) (MIT or Apache-2.0):

```yaml
# .github/workflows/discipline.yml
name: CI Sentinel
on:
  pull_request:
    types: [opened, synchronize, reopened, edited]
jobs:
  discipline:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v7
        with: { fetch-depth: 0 }
      - uses: orieg/discipline@v0
```

Merge the workflow and open one pull request so the check reports once;
GitHub lists a status check in the ruleset picker after it has run.

## 2. Require it with a ruleset

Repository **Settings → Rules → Rulesets → New ruleset → New branch ruleset**:

1. Name it, set **Enforcement status** to *Active*, target the default branch
   (**Add target → Include default branch**).
2. Tick **Require status checks to pass**, **Add checks**, and pick the job's
   check name: `gatekeep` for gatekeep, `discipline` (shown as
   *CI Sentinel / discipline*) for discipline. Tick **Require branches to be
   up to date before merging** if the gate should see the merge result.
3. Leave **Bypass list** empty. A bypass actor can merge around the gate.

The same from the CLI:

```sh
gh api -X POST repos/OWNER/REPO/rulesets --input - <<'JSON'
{"name": "test integrity", "target": "branch", "enforcement": "active",
 "conditions": {"ref_name": {"include": ["~DEFAULT_BRANCH"], "exclude": []}},
 "rules": [{"type": "required_status_checks",
            "parameters": {"strict_required_status_checks_policy": true,
                           "required_status_checks": [{"context": "gatekeep"}]}}]}
JSON
```

Rulesets apply to administrators too unless they are on the bypass list, which
is the difference from classic branch protection; they also work on private
repositories on the free plan.

## What this does and does not cover

The gate runs on the pull request, on GitHub's runner, after the night shift
has produced a branch. The box's own checks still ran agent-written code as the
appliance owner ([SECURITY.md](SECURITY.md)); the ruleset makes the test diff
itself reviewable by a tool the agent cannot edit on its way in. To run the
same tool on the box, add it as a `--check`; `GRAVE_BASE` carries the base
commit ([SCHEDULES.md](SCHEDULES.md)).
