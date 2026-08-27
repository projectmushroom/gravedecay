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

## Owner-only T3 activity

`GET /grave/api/t3-activity` is an owner/workspace-gated dashboard endpoint,
not a client API. It polls a configured T3 `GET /api/orchestration/shell`
source with a three-second cache and two-second timeout, then returns only
thread-shell metadata (identity, model, phase, plan progress, liveness, update
time, and deep link). It is deliberately absent from `/api/v1/summary` and
never requests a transcript or thread-detail endpoint. Upstream auth, timeout,
or shape failures return `unauthorized`/`unreachable` there and never delay
`/api/state`.
