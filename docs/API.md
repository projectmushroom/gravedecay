# Internal API

`GET /grave/api/v1/summary` is the stable, read-only appliance summary for
thin tailnet clients. Locally, the same route is `/api/v1/summary`. It has no
CORS header, uses `Cache-Control: no-store`, and does not add a listener or
require a token. The dashboard caches its local collection for about five
seconds.

```json
{
  "product": "gravedecay",
  "api_version": 1,
  "observed_at": "2026-08-23T12:00:00Z",
  "node": {"host": "grave", "platform": "linux", "mode": "developer", "uptime_s": 123},
  "resources": {"cpu_pct": 4.2, "memory_pct": 31.1, "disk_pct": 42.0, "cpu_temp_c": null, "gpu_temp_c": null},
  "activity": {"sessions_live": 1, "sessions_frozen": 0},
  "health": {"services_failed": 0, "containers_problem": 0, "t3": "running"},
  "links": {"dashboard": "/grave/", "t3": "/", "terminal": "/term/", "network": "/net/"}
}
```

All measurements are numbers or `null`; timestamps are UTC RFC3339. The
response intentionally contains no repository names, session/container names,
logs, accounts, configured app URLs, or other private content. On Linux,
`gaming` is intentional: stopped developer units and Docker do not contribute
to `health`; only units in the `failed` state do. The legacy macOS companion
reports only its `dashboard` and `network` paths. The optional native macOS
publisher reports no browser, T3, or terminal links: it is a bounded local
summary, not a full grave. `node.platform` is `linux`, `macos`,
or `container`. Portable (`container`) summaries intentionally return `null`
host resource and uptime values, retain `dashboard`, `t3`, and `terminal`
links, and omit `network`.

`health.t3` is an optional supervisor state: `running`, `stopped`, `starting`,
`failed`, `unknown`, or `not-configured`. Single-owner Linux uses the T3
systemd unit; source-installed macOS with agents uses its launchd job, with
a two-second query timeout. It says nothing about HTTP readiness, provider
authentication, or the viewing device's pairing. A Mac without the agents
layer reports `not-configured`; portable and multi-user backends report
`unknown`. Missing/unsupported values from older publishers display as
unknown. Offline cached summaries never establish current service state.

`links.t3_setup` is an optional dashboard path (`/`, `/grave`, or `/grave/`)
advertised by dashboards with T3 pairing controls, including macOS with the
agents layer. Clients append the fixed `#t3-setup` fragment to open those
controls. It carries no credential, makes no remote action request, and does
not establish that T3 is running or that the client is paired. Other paths,
queries, and fragments are rejected for this capability. Older summaries and
the native macOS summary publisher may omit it. Clients hide setup when a
plot is unreachable; a destination still enforces its own access checks.

## Owner-only Graveyard overview

`GET /grave/api/graveyard` returns `{state, plots, checked_at, limited}`.
Each plot contains `{id, dns, name, summary, lastSeen}`; timestamps outside the
summary are Unix seconds. `summary` is an allowlisted subset of v1 (node
host/platform/mode, CPU/memory/disk percentages, session/problem counts, and
standard app links). It contains no private work data.

Collection is asynchronous and throttled to 45 seconds. `state` is `scanning`,
`ready`, `unavailable`, `tailscale-unavailable`, or `unsupported`; `limited`
indicates the 64-candidate scan ceiling. Cached results during `scanning` are
historical. Clients keep absent saved plots as unreachable and use `lastSeen`
to label stale records. A ready result is reachability from the collector,
not proof that the requesting browser can reach the destination.

The owner identity gate applies before collection. Responses use `no-store`
and omit CORS. Portable and multi-user backends report `unsupported` without
probing. No request parameter selects a host or upstream; only validated local
Tailscale discovery supplies probe destinations. See [GRAVEYARD.md](GRAVEYARD.md).

## Owner-only benchmark

`GET /grave/api/admin/benchmark` returns run progress and the last successful
local result. `POST` accepts exactly `{"action":"start","mode":"quick"}`
(also `capacity` or `sustained`) or `{"action":"cancel"}`. Both routes require
the existing owner/admin identity gate; POST also requires the existing
same-origin protection. The macOS source companion supports them; portable
dashboards reject them. The multi-user gateway routes admin requests to the
owner backend. Results are not part of `/api/v1/summary`.
See [BENCHMARK.md](BENCHMARK.md).

## Owner-only T3 activity

`GET /grave/api/t3-activity` is an owner/workspace-gated dashboard endpoint,
not a client API. It polls a configured T3 `GET /api/orchestration/shell`
source with a three-second cache and two-second timeout, then returns only
thread-shell metadata (identity, model, phase, plan progress, liveness, update
time, and deep link). It is deliberately absent from `/api/v1/summary` and
never requests a transcript or thread-detail endpoint. Upstream auth, timeout,
or shape failures return `unauthorized`/`unreachable` there and never delay
`/api/state`.

## Owner-only updates

`GET /grave/api/admin/releases` returns the current source checkout, configured
channel, and stable release choices. A checked-out tag alone does not prove
that its installer completed or its dashboard is running.

`POST /grave/api/admin/upgrade` accepts an exact `{"tag":"vX.Y.Z"}` on
Linux and macOS. The configured-channel choice sends `{"channel":"configured"}`
on Linux, or the recorded `release`/`edge` channel on macOS. The owner and
same-origin gates apply. A successful response means **queued**, not installed.
The existing detached systemd/launchd worker performs the install.

`GET /grave/api/admin/update-status` is owner-only and uncached on both platforms.
It returns `state` (`idle`, `queued`, `running`, `ok`, `failed`), an `attempt`
identity once the worker starts, optional `message`, the last 8 KiB of the
fixed updater log, and `dashboard_current`. That last flag compares this
process's loaded Python and HTML hashes with the installed files. Logs and
update results never enter the public summary. Portable dashboards reject
these endpoints.

The web's **System → Updates & restart** dialog combines the configured-channel
and exact-release flows. It remembers an in-flight attempt in session storage,
ignores an earlier attempt's success, and reloads the page once the new attempt
succeeds and the dashboard runs its installed files. Connection failures are
retried; after 30 minutes it reports unverified completion rather than success.
Reboot is a separate confirmed Linux action, disabled during a tracked update;
macOS retains its existing restriction on host control.

Linux `grave upgrade` records its outcome in `config/update-status.json`, with
installer output in `logs/upgrade.log`. A shared `flock` prevents overlapping CLI
and dashboard updates. `grave update-status` reports interrupted running jobs
as failed when their lock has been released. The source checkout may already
have moved when an installer fails; inspect the log and rerun the same release
after fixing the reported error. This is not a Linux rollback mechanism.
Doctor checks the updater status contract and loaded dashboard files.

When upgrading from a dashboard predating this flow, reload `/grave/` manually
after the installer finishes. The old page cannot gain the new completion logic
until it loads the new HTML. If the interface is still old, run `grave doctor`
and inspect the updater output (`journalctl -u 'gravedecay-upgrade*'` on older
Linux installs) before retrying.
