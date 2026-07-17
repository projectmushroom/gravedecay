# Native clients

The box serves web UIs only (docs/PORTS.md); native clients are thin shells
over the same origins and never require new listening ports on the box.

- **iOS / macOS** — `clients/apple/`. SwiftUI app with webview panes for T3
  and the dashboard, a native SwiftTerm terminal speaking the ttyd protocol
  (the same one `web/term/app.js` documents), and an optional embedded
  Tailscale node (TailscaleKit) so a device needs no VPN profile. Build and
  distribution details live in `clients/apple/README.md`; CI is
  `.github/workflows/apple.yml` (Linux + macOS matrix).

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
