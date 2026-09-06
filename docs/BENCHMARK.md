# Development benchmark

Open the dashboard's **System → Development benchmark** panel and press
**Run benchmark**, or use the installed CLI:

```sh
grave bench                       # quick: warmup + three single-worker cycles
grave bench capacity              # 1, 2, 4, 8 workers, capped by logical CPU count
grave bench sustained             # ten minutes after the single-worker baseline
grave bench status                # status and last successful result, as JSON
grave bench cancel                # stop; keep the previous successful result
grave bench quick --json          # wait and print the complete result as JSON
```

Linux appliances and the source-installed macOS companion support both the
CLI and dashboard. The read-only native Mac publisher and portable dashboard
do not expose this control. The multi-user gateway treats this as an admin
operation on the owner dashboard; ordinary workspace users cannot start it.
This measures that runtime, not another workspace's resource quota.

For an uninstalled checkout on macOS or Linux:

```sh
python3 dashboard/benchmark.py --root /path/to/scratch-grave quick
```

Use scratch storage on the same filesystem as your development projects.
The only requirements are Python 3.9+ and Git. There are no dependency
downloads, AI calls, Docker changes, new listeners, or privileged operations.
The workload never opens your repositories. CPU/disk activity will compete
with active work, so use AC power and pause heavy jobs for a fleet comparison.

## What the score means

`grave-dev-v1` generates a fixed Python project: 128 modules with 48 functions
each and a deterministic test exercising all 6,144 functions. Each cycle:

1. Runs Git status, search, diff, and detached worktree creation/removal.
2. Forces Python bytecode compilation of the complete sample project.
3. Runs its unit tests, requiring successful execution.

One full warmup is excluded. The displayed timings are medians of three
samples. **Dev score = 1000 / median cycle seconds**, rounded to an integer:
100 points means one standard cycle takes ten seconds; 200 means five seconds.
This is a fixed workload index, not a percentile or a score calibrated against
a particular reference machine. Compare only the same suite and Python/Git
versions. Changing the fixture, commands, or score definition requires a new
suite version. Timings include subprocess startup and up to 20 ms polling
granularity per command. The filesystem is warm; compilation is forced.

This first profile measures Git/Python development work. It does not measure
TypeScript typechecking, a production application build, GPU inference,
provider latency, dependency installation, or database/browser performance.

Capacity runs three batches per worker count in independent sample repos.
Each row reports median completed cycles/minute and the median of the slowest
worker's slowdown in each batch relative to the single-worker baseline.
**Best tested workers** selects the highest-throughput tested count whose
slowdown is at most 2×. A missing value means no tested count qualified.
It is not an estimate of how many real agents fit in RAM: these fixtures are
small, with no browser, database or language server. The eight-worker cap is
intentional; it is not a claim that a large server tops out at eight agents.

Sustained mode repeats single-worker cycles for at least ten minutes, finishing
the final cycle, and reports overall cycles/minute plus the ratio of median
final-five to first-five cycle times. A ratio above 1 means later work was
slower; it does not by itself prove thermal throttling.

## Results and lifecycle

Results include hostname, architecture, OS/tool versions, logical CPU count,
host RAM and power data when available, load before/after, execution label,
raw single-worker samples, suite, timestamp and duration. RAM/CPU inventory
may describe the host rather than container/cgroup limits. Background load,
power-saving settings, virtualization, quotas and toolchain differences affect
the result; preserve these conditions when comparing boxes.

The dashboard shows the last successful result during a new run, reconnects
to progress after a page reload, and offers **Stop** and **Export JSON**.
Results live under `$GRAVE_ROOT/config/benchmark/`: `latest.json` and the last
20 dated files in `results/`. Export is local; no results are uploaded.

A process lock shared by CLI and dashboard prevents overlapping runs per
root. The runner is detached from the browser request. Each command has a
60-second timeout; quick/capacity have a five-minute budget and sustained a
15-minute budget. Cancellation terminates current workload subprocesses and
cleans temporary sample projects. A killed runner is reported as interrupted
on the next status read; an abrupt kill can leave a `work-*` scratch directory.
Previous successful results survive failures and cancellation. Reinstalling
or restarting the service can interrupt an active run.

`grave doctor` and macOS doctor-lite check runner availability and Git without
starting a workload. macOS also checks the installed runner against its managed
checkout. Benchmark controls use the existing identity and same-origin POST
checks at `/grave/api/admin/benchmark`; both status and exported results are
owner/admin-only, and are absent from the public summary API.
