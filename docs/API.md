# Direct management API (v1)

The dashboard can manage another grave without navigating to its hostname.
The browser stays on the entry dashboard origin and sends requests **directly**
to the selected grave over Tailscale. The entry machine does not forward API
traffic. This is a first management-client contract, not a proxy for T3 or
other hosted websites.

## Connect a dashboard

1. Upgrade the entry grave and each destination to a release with this API.
2. Open the destination's own dashboard. In **System → Connections &
   integrations → Trusted dashboards**, add the exact entry origin, such as
   `https://home.tail123.ts.net` (no `/grave/`, trailing slash or wildcard).
3. On the entry dashboard, open **Graveyard → Details → Manage here** for the
   destination. Its name and a persistent destination banner identify which
   grave receives operations. **Return to entry grave** restores local control.

Trust is per destination and does not synchronize. Removing an origin revokes
subsequent API requests; an already-started operation continues on its grave.
Only trust dashboard hosts you control: their JavaScript can exercise the
management permissions of your authorized Tailscale user. Configuring trust
does not authorize additional Tailscale users or expose a grave publicly.

Trust is stored in owner-private
`$GRAVE_ROOT/config/secrets/dashboard-trusted-origins.json`. The destination's
legacy, owner-gated `GET/POST /grave/api/client-trust` reads/replaces
`{"origins":["https://home.tail123.ts.net"]}` and retains same-origin mutation
protection. No versioned route can change trust. The file manager cannot access
the secret directory or rename/delete its ancestors. Origin lists are limited
to 32 exact HTTPS machine/tailnet names under `ts.net`.

## Requests and authorization

The base is `https://<destination>/grave/api/v1/` (locally `/api/v1/`).
Every management request requires `X-Grave-Client: 1` plus the destination's
existing owner identity check. On the tailnet, Tailscale Serve supplies the
requesting device's user identity. Clients must not manufacture or forward
Tailscale identity headers. Owner maintenance can use the existing private
local token on loopback. No new shared secret is distributed to browsers.

Browser requests use `credentials: 'omit'`, `mode: 'cors'` and `redirect: 'error'`.
Serve's network identity is independent of browser cookies. The exact browser
Origin must be trusted by the destination, or match the destination itself.
Native clients can omit Origin and still require the owner check and protocol
header. OPTIONS preflights accept only the advertised method and the
`Content-Type` / `X-Grave-Client` headers. An allowed origin is returned exactly,
with `Vary: Origin`; there is no wildcard, credentialed CORS, or preflight cache.
Each actual request rechecks trust and identity. Legacy routes retain their
existing protections and do not acquire CORS headers.

This first version supports **single-owner Linux and macOS**. Multi-user
backends and portable workspaces fail closed (501, or their existing backend
capability refusal). Their workspace identity delegation is future work.

## Contract

`GET capabilities` returns:

```json
{
  "product": "gravedecay",
  "api_version": 1,
  "host": "destination",
  "platform": "linux",
  "routes": {"GET": ["capabilities", "state", "settings"], "POST": ["settings"]},
  "actions": ["doctor"]
}
```

The route/action lists above are illustrative; use the actual advertised lists.
Clients must tolerate added fields and hide/refuse unsupported operations.
Platform-specific handlers continue to validate runtime availability.

| Routes | Method / response |
| --- | --- |
| `state` | GET: authorized dashboard snapshot, with the existing platform-specific fields; no public-state fallback |
| `settings` | GET: `{settings: {...}}`; POST: existing validated settings patch and `{ok, settings, ...}` response |
| `admin/benchmark` | GET status; POST start/cancel with existing mode validation |
| `admin/releases`, `admin/update-status` | GET release choices and update progress |
| `admin/upgrade`, `admin/update-check` | POST existing validated update request; update-check only on native Mac hosting |
| `t3-activity`, `dispatch-pr`, `agent-log` | GET existing bounded activity, PR and log projections |
| `files`, `download` | GET jailed directory listing / binary download |
| `fs`, `upload` | POST jailed file operations / raw file upload |
| `session-kill`, `session-resume`, `session-capture`, `agent-job-cancel` | POST the existing named-session/job operation |
| `linear-issue`, `linear-dispatch`, `notify-test` | POST existing integration operations |
| `operations` | GET owner-private progress or health; POST a retry-safe console action (single-owner Linux) |
| `action` | POST `{action: ...}` using the advertised fixed action names |
| `action-stream?action=...` | **POST only**: SSE output (`data: <JSON string>`, then `event: done` with exit code) |

These routes share the local dashboard's validated handlers and payloads;
legacy clients continue to work. The web dashboard uses the v1 transport for
remote management and preserves legacy local requests for workspace/portable
compatibility. This first version exposes a dashboard-shaped state snapshot;
structured resources are documented below, while integrations retain their existing dashboard-shaped contracts.

Gate failures use `{ok:false,error:<code>,output:<explanation>}`:
`forbidden`, `untrusted_origin`, `client_header_required` (403),
`unsupported_route` (404), or `unsupported` (501). Operation handlers keep their
existing response/error bodies. Clients must check HTTP status before rendering.
The public `summary` endpoint below is unchanged and remains without CORS.

## Client behavior and limits

The selected destination is an explicit `grave` query parameter on the entry
page. Changing it reloads the same dashboard origin, so pending requests cannot
be retargeted by a selection change. Remote mode ignores the entry page's
server-rendered snapshot, never falls back to local requests, and partitions
update tracking and panel preferences by destination. Discovery still uses the
entry grave's existing inventory collector. The entry host must be reachable to
load/reload the shell; an independently cached client shell is future work.

An old, unauthorized or unreachable destination remains selected with a visible
error and a direct setup link. Browser CORS/network failures can be
indistinguishable; the UI directs the user to check Tailscale, destination trust
and API version. Browser push enrollment stays on the destination's own PWA.

T3, terminal and other app links still open the selected grave's own origin.
Custom modal apps open directly when managing remotely. The management API does
not eliminate iOS browser chrome when navigating to those websites. Direct
terminal/T3 protocol integrations are separate follow-ups. Remote file downloads
currently assemble a Blob in the client and are limited to 64 MiB; use the
destination directly for larger files. Local downloads retain browser streaming.

Console actions advertised in `capabilities.operations.actions` use the durable
operation protocol below. Other mutations are not automatically retried. Existing
update/benchmark status can be queried after reconnect. An interrupted legacy
stream does not cancel its server operation.
`grave doctor`'s dashboard auth probe checks version/capabilities, operation journal
integrity and runner build identity, plus refusal of unauthorized identities and
untrusted origins, without starting actions or changing trust.

## Structured resources and OpenAPI

The [OpenAPI 3.1 description](openapi.json) is also available from the destination
at `GET openapi.json`, behind the same owner/origin/protocol checks. It describes
the v1 catalogue; **capabilities remains the runtime availability authority**.
`capabilities.resource_contract` gives the contract version (`1.0.0`), schema URL,
canonical schema SHA-256, implementation build digest, and available resource names.
The document follows the [OpenAPI 3.1.1 specification](https://spec.openapis.org/oas/v3.1.1.html).

Tailnet owner authorization is out of band and is documented in the schema's
`x-grave-authorization` extension. It is mandatory for every operation. There is
no anonymous access or browser login cookie. `X-Grave-Client: 1` is only a protocol
marker. Generated clients must **never set Tailscale identity headers**; Serve
supplies them. The private local maintenance token remains a loopback alternative.

| Resource route | Representation |
| --- | --- |
| `GET resources/system` | Host identity, uptime in seconds, CPU percentage/count, memory and disks in bytes, load averages, temperatures in Celsius |
| `GET resources/services` | Service IDs, supervisor, machine state and optional detail |
| `GET resources/containers` | Container names/IDs, machine state, optional status text and Compose project |
| `GET resources/sessions` | Session names/IDs, integer window counts, nullable attached state, frozen state and optional activity label |
| `GET resources/repositories` | Local inventory with IDs, branch, changed-file count and optional commit subject |
| `GET/POST resources/preferences` | Dashboard preferences and their revision; POST applies a validated patch |

These routes support single-owner Linux and macOS. macOS advertises sessions only
with its agents layer. Portable and multi-user backends remain unsupported. There
are no unversioned `/api/resources/*` aliases. Each read runs only its own collector;
reading system metrics never scans repositories or requests GitHub/Linear data.

Resource responses share an envelope:

```json
{
  "api_version": 1,
  "kind": "services",
  "observed_at": "2026-09-20T12:00:00Z",
  "status": "ready",
  "data": [{"id":"t3code","manager":"systemd","state":"active","detail":"running"}],
  "error": null,
  "truncated": false
}
```

`status` is `ready`, `partial`, `unavailable`, or `paused`. A ready empty list means
no entries; unavailable/paused data is `null`, with an error `{code,message}`.
Partial results can contain useful data even when a scan was incomplete or a core
system measurement is missing. Individual unknown measurements are `null`, never
fabricated zeroes; machine-readable states are separate from human-readable labels.
macOS memory percentage measures **pressure**, so `memory.percent_kind` is
`pressure` and `used_bytes` is `null`. Linux reports occupied memory (`used`).

IDs are scoped to the destination origin. `system.identity.id` is `self`; service
IDs are supervisor names, container IDs are names, session IDs are tmux names,
and repository IDs are names relative to the configured inventory root. Renaming
one changes its ID; container names are not Docker immutable object IDs. Session
`activity_label` is display text, not a timestamp. These collection IDs do not
implicitly authorize arbitrary service/container commands.

Collections return at most 500 entries (system disks at most 32), setting
`truncated` for capped collections. macOS inventory retains its existing smaller
scan limits. There is no pagination in this revision. In gaming mode, repositories
and containers report `paused` without running their collectors. Metrics, services
and containers cache representations for five seconds, sessions for two, and
repositories for fifteen. Some underlying collectors also cache samples:
`observed_at` is the time this representation was assembled, **not a guarantee
that every underlying measurement was sampled then**. Concurrent requests for the
same resource share one collection. HTTP responses remain `no-store`.

### Preference writes

Read `resources/preferences` before editing. Its `data` contains `revision` and
`values`. Send that revision with only the fields being changed:

```json
{"revision":"<64 lowercase hex digits from GET>","changes":{"poll_ms":10000}}
```

The server validates the complete patch before writing, checks the revision and
atomically replaces the settings file. Both this endpoint and legacy settings
saves share one write lock in the installed dashboard process. A stale revision
returns 409 `revision_conflict` without modifying anything. A lost success response
is resolved by reading again and inspecting the values, not by generating a blind
replacement. Arbitrary external edits/file-manager writes are outside this write
protocol; operate one dashboard process per grave as installed.

Requests are limited to 64 KiB of encoded JSON. Supported fields are `panel_order`, `hidden_panels`, `hidden_apps`, `newtab_apps`,
`modal_apps`, `yolo_apps`, `custom_apps`, `t3_tile`, and `poll_ms`. Unknown fields,
wrong types, duplicate list entries, out-of-range intervals, unsafe tile URLs and
credential-bearing URLs are rejected. The OpenAPI schema defines bounds; URL
validation additionally enforces the documented semantic rules. Integration secrets,
repository-root configuration and notification settings stay on their existing
handlers. They cannot be written through the preference resource. Invalid legacy
list entries and credential-bearing tiles are omitted from its typed view.

The dashboard preference editor reads this resource when opened, then submits its
revision and changed fields. Conflicts keep the draft visible and ask the user to
reopen and review. Network/authorization/validation errors never fall back to the
legacy settings writer. Explicitly older peers without the resource capability,
and unsupported local workspace deployments, retain their legacy editor behavior.
Other settings sections continue to use their existing handlers.

### Compatibility and validation

`/api/v1` is the API major version. The structured contract uses semantic versions:
patch releases clarify/fix behavior without changing the contract, and minor
releases may add routes and optional fields. Clients must ignore unknown response
fields and discover available features instead of comparing entire capability
lists. Removing fields/routes, changing field types or units, introducing required
request fields, or changing closed enum values requires a new major API version.
Open string state fields can acquire new values; render unfamiliar states as
unknown. Do not assume newer clients can call every method on an older grave.

Structured resources, capabilities and operations have concrete schemas marked
`x-grave-contract: 1.0.0`. Existing dashboard-shaped endpoints are explicitly marked
`legacy`; their generic object schemas are not promises of normalized resource
fields. The original state snapshot remains available while integration and other
clients migrate. No v1 method is removed by this phase.

`grave doctor` checks schema and implementation digests, resource discovery and
preference shape without changing configuration. To validate/regenerate contracts
in a development checkout (JSON Schema/OpenAPI validators are test dependencies):

```sh
python -m venv .venv
.venv/bin/python -m pip install -r tests/requirements.txt
.venv/bin/python -m unittest discover -s tests -v
python dashboard/api_contract.py --check docs/openapi.json
python dashboard/api_contract.py > docs/openapi.json  # after an intentional contract change
```

Tests validate the actual HTTP representations against their JSON Schemas, validate
the OpenAPI document, and compare the committed snapshot with the generated one.

## Resumable console operations

On single-owner Linux graves, `capabilities.operations` advertises `protocol: 1`,
`actions`, `retention_seconds` and `new_id_max_age_seconds`. Only use actions also
advertised by the destination. Current coverage is doctor, gaming/developer mode,
T3 restart/update, T3 Connect off and reboot. Pairing remains an ephemeral stream:
its token and pairing URL must not enter operation history. Appliance updates and
benchmarks keep their dedicated status APIs. Other platforms retain their existing
handlers; durable coverage can grow without changing older clients.

Before submitting, save a unique ID on the client: Unix seconds, a hyphen and
32 lowercase UUID hex digits, e.g. `1790000000-3bce7ad71fa04458bd43bdd0f9864d49`.
The timestamp must be within the preceding 24 hours or at most five minutes ahead
of the destination clock for a **new** operation. A known ID can be read/retried
throughout its retention period. Persist the ID *before* sending this JSON:

```http
POST /grave/api/v1/operations
Content-Type: application/json
X-Grave-Client: 1

{"id":"1790000000-3bce7ad71fa04458bd43bdd0f9864d49","action":"doctor"}
```

A new operation returns 202; the same ID/action returns 200 with the existing
record, without running again. Reusing an ID for a different action returns 409
`id_conflict`. A different action already holding the shared action lock returns
409 `busy` without creating a record. Caller-provided commands/arguments are never
accepted. The record is atomically persisted and synced before execution starts.

`GET operations?id=<id>&after=<cursor>` returns:

```json
{
  "id": "1790000000-3bce7ad71fa04458bd43bdd0f9864d49",
  "action": "doctor",
  "state": "running",
  "created_at": 1790000000.1,
  "finished_at": null,
  "exit_code": null,
  "cursor": 2,
  "events": [{"seq": 2, "text": "Checking services…\n"}],
  "truncated": false,
  "message": ""
}
```

Events are ordered text chunks, not necessarily whole lines; `after` excludes
already-read sequence numbers. Poll about once a second while viewing progress.
States are `queued`, `running`, `succeeded`, `failed`, `timed_out`, `interrupted`.
A zero command exit means `succeeded`; service readiness may still need its normal
state check. No percentage is fabricated for commands without measurable progress.
All reads, starts and retries recheck identity/origin trust. Local owner clients
can use `/api/operations` with the existing same-origin protection. GET never
starts a command. `GET operations?health=1` is the private, read-only doctor probe.

The dashboard stores only the latest operation's ID/action/tracking state per
destination in browser local storage. **Resume action** reopens progress after
closing the console or reloading. If the initial POST response was lost, it first
looks up that ID; only a missing, never-acknowledged ID is submitted again, with
exactly the same ID. An acknowledged operation that disappears is never recreated.
Completed progress remains available through **View last action**. Output and
credentials are not cached in browser storage. Other mutations retain their
existing behavior; a settings save is not an operation-history entry.

Output is limited to 128 KiB of UTF-8 or 512 chunks per operation, with a visible
`truncated` flag. The runner continues draining discarded output and enforces a
20-minute timeout, including silent commands and descendants holding stdout open.
Records live in owner-private `$GRAVE_ROOT/config/secrets/operations/` (directory
0700, atomic record files 0600), protected from the dashboard file manager. Keep
this directory private in backups: command diagnostics may contain private data.
There are at most 512 records. Records older than seven days are pruned on the
next accepted start; a full journal returns 503 `history_full`. Unknown old IDs
return 409 `expired_id`, so pruning cannot turn an old retry into a fresh command.
Missing/expired reads return 404 `not_found`; unreadable history fails closed.
Do not delete this directory to retry an uncertain action: doing so removes its
idempotency record. No API exposes history deletion.

Browser disconnects do not stop commands. A dashboard restart reports unfinished
records as **interrupted / outcome unknown**, retaining output, and never replays
them. A surviving command retains the file lock and blocks another console action
until it exits; systemd may instead terminate the service's whole process group.
Check the grave's state before deliberately starting a new action with a new ID.
This is retry deduplication and durable observation, not a guarantee of exactly-once
side effects across a power failure. Reboot commonly produces an interrupted
record even when the reboot itself succeeds. The store assumes one dashboard
process per grave, as installed by systemd/LaunchAgent.

# Existing dashboard API

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
to `health`; only units in the `failed` state do. The default classic macOS companion and standalone Mac app host
report `dashboard` and `network` paths. The classic agents layer additionally
advertises its T3 and terminal services. `node.platform` is `linux`, `macos`,
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
the standalone Mac app host omits it because it does not run a local T3 service. Clients hide setup when a
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
The existing detached systemd/launchd worker performs classic installs. Native
Mac hosting forwards an exact offered tag (or `{"channel":"release"}`) to the
app's Sparkle updater; it never invokes the classic companion worker.

`GET /grave/api/admin/update-status` is owner-only and uncached on both platforms.
It returns `state` (`idle`, `queued`, `running`, `ok`, `failed`), an `attempt`
identity once the worker starts, optional `message`, the last 8 KiB of the
fixed updater log, and `dashboard_current`. That last flag compares this
process's loaded Python and HTML hashes with the installed files. Logs and
update results never enter the public summary. Portable dashboards reject
these endpoints. Native Mac status uses the same attempt/state contract and
also exposes `current`, `latest`, `releases`, `available`, and `checking`.
Its `dashboard_current` confirms the requested app version after relaunch.
`POST /grave/api/admin/update-check` with `{}` requests a fresh native app feed
check. It is owner-only and same-origin, and returns 404 on other installations.
Native updater status is read from a private, bounded, heartbeat-checked file;
the web backend can queue a release identifier but cannot supply an executable
or download URL. Concurrent requests are rejected instead of overwriting one
another. A stale or missing app updater fails closed.

The dashboard shows a latest-release notice with a confirmed quick update
action. **System → Updates & restart** opens the release picker. The dialog combines the configured-channel
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

## Shared web icon

The dashboard, PWA manifest, Apple touch icon, notification icon, and favicons
use the same skull artwork as the README and native clients. Committed PNGs
at 16, 32, 180, 192, and 512 pixels live with the dashboard static files, so
Linux, macOS, and portable installs need no image library to serve the declared
sizes. Icon URLs include an artwork digest to invalidate browser caches.
Doctor checks the PWA PNG dimensions. An explicit `GRAVEDECAY_ICON` retains
the existing custom-PNG override.

After changing `assets/gravedecay-skull.svg`, run
`sh assets/generate-web-icons.sh` on macOS and commit the exports. This also
refreshes the legacy installed PNG and the optional T3 branding assets; the
existing `assets/apply-t3-icon.sh` remains their reapply mechanism.


## Denied-viewer dashboard state

`GET /grave/api/state` and the initial `/grave/` page use one access decision
before any platform or mode-specific owner collectors run. An unlisted tailnet
login, a headerless request, or an invalid local maintenance token receives
only these fields:

```json
{
  "access": "read-only",
  "host": "my-box",
  "platform": "linux",
  "mode": "developer",
  "now": "12:34:56",
  "viewer": "viewer@example.com",
  "resources": {
    "uptime_s": 3600,
    "cpu_pct": 12,
    "memory_pct": 34,
    "disk_pct": 56,
    "cpu_temp_c": 60,
    "gpu_temp_c": null
  }
}
```

The field set is closed. Resource values are finite numbers or `null`; portable
workspaces return `null` for every resource instead of reading host metrics.
There are no owner settings, configured app URLs, detailed OS metadata,
service/container inventory, session names, integration data, logs, or backup
metadata. Clients must check `access` before assuming the owner state schema.
The dashboard renders a read-only vitals panel for this response, including
when an already-open owner page loses access.

Authorized owner and local maintenance responses keep their existing schema.
The stable `/api/v1/summary` contract is unchanged. Private reads, mutations,
files, CSRF checks, and multi-user backend capabilities retain their separate
gates. Linux/macOS companion `--check-auth` and the native Mac host doctor
validate the live denied-viewer schema. Apply the update through the normal
appliance re-raise, portable image recreation, or Mac application/companion update.
