# Dashboard layout

`/grave/` is the mobile PWA entry point. The compact header identifies the
current grave and opens Graveyard. **Work** and **System** stay at the bottom
on phones, above the safe area; larger screens keep navigation above the content.

## Work

The quick launcher shows T3 and Terminal when available, falling back to the
first available apps. **More apps** exposes the rest. Launcher visibility is
editable under **System → Dashboard preferences**. Apps remain in Work.

**Needs attention** links to requested reviews, failed CI runs, failed overnight
runs and failed services reported by the available data sources. These are
reported states, not inferred agent liveness. Selecting a notice opens its
section, including a normally hidden widget, without changing saved visibility.

The default order is active work (T3 and terminal sessions), work queue (PRs,
Linear and CI), repositories and usage, then recent activity (overnight reports,
session history and notification history). Active sections initially show up
to three records when present; **Show all** opens the full list. Empty active
sections start closed. End-session controls live beside terminal sessions.

Other sections start with a summary: counts, status, quota usage or latest
result. Tap the labelled section header to show details. Warning summaries
remain visible when details are closed. An unconnected Linear section offers
**Connect Linear** in its details.

Expansion is saved in browser storage per origin and dashboard path, so it is
specific to this device and grave/workspace. Refreshes update summaries without
reopening sections or reordering them. Explicit expansion choices survive
reload. The existing shared widget visibility and order are preserved; order
is applied within the new groups. The old stock ordering adopts the new default
without rewriting its settings file.

## System

- **Health:** resource summary, services, Docker and diagnostics. Expand
  Diagnostics to run doctor or read journal errors from the past 24 hours.
- **Maintenance:** Updates & restart, followed by Development benchmark.
  Updates opens the release picker directly. Benchmark starts with the last
  score, workload and measurement date (or “Not run yet”). Expand for workload
  selection, timings and export. Progress and Stop remain visible during a run,
  including when details are closed. A score is a past synthetic measurement,
  not live capacity; see [BENCHMARK.md](BENCHMARK.md).
- **Configuration:** Connections & integrations, Notifications, Machine
  behavior and Dashboard preferences. Each opens one focused configuration
  view with Back and Save, keyboard focus containment, and a scope explanation.
  Existing immediate actions (pairing, boot mode, push enrollment, etc.) retain
  their immediate behavior. Back discards unsaved form changes.

The separate gear and duplicate settings update entry are removed. Pairing,
T3 Connect and provider authentication live in **Connections & integrations**.
Gaming, boot mode, auto-throttle and keepalive live in **Machine behavior**.
Launcher behavior, widget visibility/order and polling live in **Dashboard
preferences**. Push enrollment applies to this device; notification event and
relay preferences apply to the grave. Launcher and widget preferences remain
shared by the grave, unlike local expansion state.

The latest-release notice remains a shortcut to the same updater. Existing
`?tab=system` and `#t3-setup` links continue to work. Gaming mode retains System
configuration and updates so the grave can still be managed.

## Host and access differences

Portable workspaces label the second destination **Preferences** and retain
workspace integration/dashboard configuration without host telemetry, benchmark,
restart, notification or machine controls. macOS preserves its supported local
work and configuration controls. Public viewers see only the existing numeric
status page; private navigation, content and configuration stay hidden and API
authorization remains enforced by the server.

`grave doctor` checks that the installed dashboard includes mobile navigation
and configuration entry points, and separately verifies that the running process
serves the installed shell hash. Browser coverage exercises phone/tablet layouts,
expansion persistence, refresh stability, focused configuration, benchmark
progress, portable workspaces and public access transitions.

## Phone examples

Rendered at 390 × 844 with example work and benchmark data:

[Work](../assets/dashboard-mobile-work.png) ·
[Healthy System](../assets/dashboard-mobile-system.png) ·
[System with a failed service and running benchmark](../assets/dashboard-mobile-system-busy.png)
