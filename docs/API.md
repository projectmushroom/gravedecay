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
  "health": {"services_failed": 0, "containers_problem": 0},
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
