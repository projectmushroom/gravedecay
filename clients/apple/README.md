# Gravedecay for iOS & macOS

One native app for the box's three web surfaces — T3 (`/`), the dashboard
(`/grave/`), and the terminal — plus a **native SwiftTerm terminal** that
speaks the ttyd 1.7.7 websocket protocol directly (same protocol as
`web/term/app.js`, see docs/TERMINAL.md), and an optional **embedded
Tailscale node** so the app needs no VPN profile at all.

## Layout

| Piece | What |
|---|---|
| `GravedecayKit/` | SwiftPM package: ttyd protocol + flow control, box URL layout, websocket transport. Platform-independent, tested on Linux and macOS in CI. |
| `App/Sources/` | SwiftUI app (one codebase, iOS 17+ / macOS 15+). Webview panes, native terminal, settings, and the macOS menu bar. |
| `project.yml` | XcodeGen spec — base build, connectivity via the Tailscale VPN app. |
| `project-embedded.yml` | Overlay adding TailscaleKit (in-app tailnet node). |

## Build (on a Mac)

```sh
brew install xcodegen
make project          # → Gravedecay.xcodeproj, open it in Xcode
```

Embedded-tailnet build (needs Go, clones + builds tailscale/libtailscale):

```sh
make project-embedded
```

The app code adapts via `#if canImport(TailscaleKit)` — both projects build
from the same sources.

## macOS menu bar

The macOS app is menu-bar-only (`LSUIElement`): it has no Dock icon, but
**Open Gravedecay** brings forward the normal native app window (T3,
dashboard, and terminal). On launch, opening the menu, manual refresh, and
about every 45 seconds it runs `tailscale status --json`, preferring
`/usr/local/bin/tailscale` then the CLI inside Tailscale.app. It sets
`TAILSCALE_BE_CLI=1` and never changes Tailscale login, Serve, or preferences.

Only online Self/Peer nodes with a stable node ID and a strict DNS name are
probed at `https://<dns>/grave/api/v1/summary`. Responses time out in three
seconds and are capped at 64 KiB; the app displays only the versioned
`gravedecay` summary contract. Previous valid summaries remain visible as
unreachable if a later probe fails. Dashboard, T3, Terminal, and Network
actions are constructed only from the selected DNS name and safe same-host
single-slash paths supplied by that contract.

The direct-distribution macOS target deliberately is not App Sandbox enabled:
running the user-installed Tailscale CLI requires local process access. It
uses no credentials, listener, daemon, registry, analytics, or remote control;
network requests remain HTTPS tailnet requests. Hardened Runtime remains on
for release builds.

## Connectivity modes

- **Tailscale app (VPN)** — default. The device is already on the tailnet;
  the app just loads `https://<box>.ts.net/…`.
- **Embedded (in-app node)** — TailscaleKit runs a userspace tsnet node
  inside the app and vends a loopback SOCKS5 proxy. Webviews are routed
  through it via `WKWebsiteDataStore.proxyConfigurations`, the terminal
  websocket via `URLSessionConfiguration.proxyConfigurations`. First join
  needs a Tailscale auth key (admin console → Settings → Keys); the node
  identity persists in Application Support, the key is never stored. The
  node appears as `gravedecay-app` in the tailnet admin panel.

## Terminal

Native SwiftTerm view attached to the same tmux socket as the web terminal
and SSH (`bin/webterm` behind ttyd). OSC 52 copies (tmux `set-clipboard on`)
land on the system pasteboard via SwiftTerm's `clipboardCopy` delegate;
clipboard reads are never answered. Reconnects forever with the same backoff
as the web client — sessions live in tmux, not in the app.

## Testing

```sh
make test                        # GravedecayKit unit tests (macOS or Linux)
# on the box (no Swift toolchain):
docker run --rm -v "$PWD/GravedecayKit:/pkg" -w /pkg swift:6.0 swift test
```

CI (`.github/workflows/apple.yml`) runs the package tests on Linux + macOS
and builds both app targets unsigned. The release app is Universal 2.

```sh
make project
xcodebuild -project Gravedecay.xcodeproj -scheme Gravedecay-macOS \
  -configuration Release build CODE_SIGNING_ALLOWED=NO ARCHS="arm64 x86_64"
lipo -info ~/Library/Developer/Xcode/DerivedData/Gravedecay-*/Build/Products/Release/Gravedecay.app/Contents/MacOS/Gravedecay
```

## Distribution notes

- macOS: this first workflow is unsigned local Release output; Developer ID,
  notarization, and DMG packaging are intentionally not included yet.
- iOS personal: development signing / TestFlight ($99 dev account).
- iOS public (EU): AltStore PAL self-publishing — Apple notarization only,
  host the signed package ourselves.
