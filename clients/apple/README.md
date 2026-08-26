# Gravedecay for iOS & macOS

iOS is a compact client for the box's web surfaces and native SwiftTerm
terminal. macOS is a standalone, Universal 2 native application: it lives in
the Dock and menu bar, renders its System, Work, Network, Appliances, and
Settings surfaces in SwiftUI, and uses no local Python service or webview.
T3 and a box's full legacy dashboard deliberately open in the default browser.

## Layout

| Piece | What |
|---|---|
| `GravedecayKit/` | SwiftPM package: ttyd protocol + flow control, box URL layout, websocket transport. Platform-independent, tested on Linux and macOS in CI. |
| `App/Sources/` | SwiftUI app (iOS 17+ / macOS 15+). iOS web panes; macOS-native dashboard, menu bar, browser hand-offs, and SwiftTerm. |
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

## Native macOS app

The macOS app is a normal Dock app with a menu-bar summary. Its native
window reads unprivileged macOS system state, scans a chosen work folder for
Git repositories, reads GitHub CLI authentication status, optionally reads
assigned Linear issues using an app-owned Keychain key, discovers tailnet
appliances, and hosts the native terminal. It opens T3 and legacy dashboards
in the default browser; `WebPane` is compiled only for iOS.

On launch, opening the menu, manual refresh, and about every 45 seconds it runs `tailscale status --json`, preferring
`/usr/local/bin/tailscale` then the CLI inside Tailscale.app. It sets
`TAILSCALE_BE_CLI=1` and never changes Tailscale login, Serve, or preferences.

Only online Self/Peer nodes with a stable node ID and a strict DNS name are
probed at `https://<dns>/grave/api/v1/summary`. Responses time out in three
seconds and are capped at 64 KiB; the app displays only the versioned
`gravedecay` summary contract. Previous valid summaries remain visible as
unreachable if a later probe fails. Dashboard, T3, Terminal, and Network
actions are constructed only from the selected DNS name and safe same-host
single-slash paths supplied by that contract. The persistent target picker
includes **This Mac** and discovered graves. A native terminal is created only
when the selected grave explicitly advertises the standard `/term` capability;
This Mac does not.

Settings → **Advertise This Mac** is off by default. It starts a small native
loopback-only (`127.0.0.1:4712`) GET/HEAD server for `/healthz` and
`/api/v1/summary` while the app runs. It refuses an occupied port or the
legacy `io.gravedecay.dashboard` companion, and does not alter Tailscale,
Serve, login, or preferences. The UI shows a manual Serve command instead,
because safely removing a path from a shared Serve configuration cannot be
proven after a restart.

The direct-distribution macOS target deliberately is not App Sandbox enabled:
running the user-installed Tailscale CLI requires local process access. It
uses no credentials, daemon, registry, analytics, or remote control; its
optional listener is loopback-only and explicit. Network requests remain HTTPS
tailnet requests. Hardened Runtime remains on
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
clipboard reads are never answered. The terminal header exposes token,
connection, reconnect, and error state with Retry and bounded redacted
diagnostics; close errors stop automatic retries instead of disappearing.

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

`make package-dmg` creates an unsigned, mountable Universal 2 DMG at
`build/Gravedecay-macOS.dmg`; CI publishes that artifact. Set
`SIGNING='CODE_SIGNING_ALLOWED=YES CODE_SIGN_IDENTITY="Developer ID Application: …"'`
and `NOTARY_PROFILE` to a configured `notarytool` Keychain profile for a
signed, verified, notarized, and stapled release. Notarization refuses an
unsigned app; a Developer ID certificate/profile remains a release prerequisite.
- iOS personal: development signing / TestFlight ($99 dev account).
- iOS public (EU): AltStore PAL self-publishing — Apple notarization only,
  host the signed package ourselves.
