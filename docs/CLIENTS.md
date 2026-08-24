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

- **iOS / macOS** — `clients/apple/`. SwiftUI app with webview panes for T3
  and the dashboard, a native SwiftTerm terminal speaking the ttyd protocol
  (the same one `web/term/app.js` documents), and an optional embedded
  Tailscale node (TailscaleKit) so a device needs no VPN profile. Build and
  distribution details live in `clients/apple/README.md`; CI is
  `.github/workflows/apple.yml` (Linux + macOS matrix).
  On macOS 15+ the same app also has a menu-bar summary and discovery surface.
  It invokes the local Tailscale CLI read-only, validates online nodes and the
  versioned summary before display, and opens only same-host HTTPS paths from
  the validated response.

- **Omarchy 4 / Quattro** — `clients/omarchy/` is a native Quickshell bar
  widget. It reads local `tailscale status --json`, probes online peers at
  `/grave/api/v1/summary`, and keeps only reachable summaries in shell memory.
  It needs Tailscale and `curl`; see its README for installation and local
  development. It never stores tailnet inventory, credentials, or remote
  content, and it cannot control an appliance.

House rules that apply to any future client:

- Talk to the box only through its tailnet HTTPS origin — never add or
  expect a non-loopback listener.
- Auth material (Tailscale auth keys, pairing tokens) is entered by the
  human at enrollment and never persisted by the client; long-lived state
  is limited to what the platform keychain/state dir provides (the tsnet
  node identity, webview cookies).
- A client change that needs a server-side counterpart follows the normal
  rule: update the matching doc and add a `grave doctor` check in the same
  commit.
