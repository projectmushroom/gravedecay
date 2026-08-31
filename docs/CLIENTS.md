# Native clients

The box serves web UIs only (docs/PORTS.md); native clients are thin shells
over the same origins and never require new listening ports on the box.

- **The official T3 Code apps** (iOS / Android / desktop) are first-class
  clients for the *T3 half* of the appliance and usually the nicest way to
  drive agents from a phone. Two ways to hook them up:
  1. **Pairing token over the tailnet** — Tailscale on the device, then
     ⚙️ settings → 🔑 New T3 pairing token, same as any browser. No Connect
     account needed.
  2. **T3 Connect** — `grave t3 connect publish` (notifications/Live
     Activities while transport stays the tailnet) or `full` (managed relay,
     no VPN needed on the device). See the trust note in SECURITY.md; doctor
     enforces the declared mode.
  The official apps drive T3 only — the dashboard (system overview, controls,
  files, terminal, gravenet) stays on the tailnet origin / PWA.
  A Mac raised with the agents layer (`macos/install.sh --agents`,
  docs/MACOS.md) serves the same origin layout (`/` → T3, `/term` →
  terminal), so pairing tokens minted from its ⚙️ settings enroll phones and
  the official apps exactly like an appliance.

- **iOS / macOS** — `clients/apple/`. iOS uses webview panes for T3 and the
  dashboard. macOS 15+ is a standalone native SwiftUI Dock/menu-bar app:
  Graveyard selects validated remote graves, with native This Mac, Work,
  Network, Terminal, and Settings surfaces. It opens only T3 in the default
  browser. Tailscale discovery is read-only; Graveyard, Settings, and the menu
  bar can open Tailscale for you to sign in. Both platforms use native
  SwiftTerm and reach the box through the Tailscale app. Build and
  distribution details live in `clients/apple/README.md`.

- **T3 activity in the dashboard** is opt-in. Create the owner-only
  `$GRAVE_ROOT/config/secrets/t3-activity.env` (mode 600), then restart the
  dashboard:

  ```sh
  T3_ACTIVITY_URL=https://mac.ts.net       # or http://127.0.0.1:4711
  T3_ACTIVITY_TOKEN=...                    # T3 orchestration:read only
  T3_ACTIVITY_ENVIRONMENT="Mac T3"
  T3_ACTIVITY_ENVIRONMENT_ID=...
  ```

  A remote source must be tailnet HTTPS. The dashboard polls T3's shell
  projection every few seconds and links to the thread; an expired/revoked
  bearer renders as unavailable until it is re-paired/replaced. This is
  dashboard-only metadata, never a notification or peer-summary feed.

- **Omarchy 4 / Quattro** — `clients/omarchy/` is a native Quickshell bar
  widget. It reads local `tailscale status --json`, probes online peers at
  `/grave/api/v1/summary`, and keeps only reachable summaries in shell memory.
  It needs Tailscale and `curl`; see its README for installation and local
  development. It never stores tailnet inventory, credentials, or remote
  content, and it cannot control an appliance.

House rules that apply to any future client:

- Talk to the box only through its tailnet HTTPS origin — never add or
  expect a non-loopback listener.
- Auth material (pairing tokens) is entered by the human at enrollment and
  never persisted by the client; long-lived state is limited to what the
  platform keychain/state dir provides (webview cookies).
- A client change that needs a server-side counterpart follows the normal
  rule: update the matching doc and add a `grave doctor` check in the same
  commit.
