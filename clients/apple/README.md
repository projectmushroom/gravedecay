# Gravedecay for iOS & macOS

iOS is a compact client for the box's web surfaces and native SwiftTerm
terminal. macOS is a standalone, Universal 2 native application: it lives in
the Dock and menu bar, renders Graveyard, This Mac, Work, Network, Terminal,
and Settings in SwiftUI. It can host the shared Mac web dashboard/PWA and
network monitor using its bundled Python backend; no separate companion or
Python installation is needed. Dashboard and T3 links open in the default browser.

## Layout

| Piece | What |
|---|---|
| `GravedecayKit/` | SwiftPM package: ttyd protocol + flow control, box URL layout, websocket transport. Platform-independent, tested on Linux and macOS in CI. |
| `App/Sources/` | SwiftUI app (iOS 17+ / macOS 15+). iOS web panes; native macOS surfaces, menu bar, T3 hand-off, and SwiftTerm. |
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

The macOS app is a normal Dock app with a menu-bar summary. First run starts
remote-first: **Connect to Graves** discovers tailnet graves; **Share This
Mac** opens Settings for the opt-in local host. You can do both later. The
native window reads unprivileged macOS system state, scans a chosen work folder
for Git repositories, reads GitHub CLI status, optionally reads assigned
Linear issues from an app-owned Keychain key, discovers tailnet graves, and
hosts the native terminal. It opens Dashboard and T3 in the default browser; `WebPane`
is compiled only for iOS.

The look is the appliance's grave dark, deliberately and in native form:
`GraveTheme` is the single palette (phosphor greens on near-black, amber
accents), `GraveAtmosphere` lays the scanline/vignette wash, and surface
headers are all-caps monospaced (`GRAVEYARD // TAILNET`). Build new surfaces
from `GravePanel`/`GraveButton`/`GraveMeter` on `GraveTheme` colors rather
than system-styled SwiftUI defaults, so the app keeps reading like the
dashboard and not like a preferences pane.

On launch, opening the menu, manual refresh, and about every 45 seconds it runs `tailscale status --json`, preferring
`/usr/local/bin/tailscale` then the CLI inside Tailscale.app. It sets
`TAILSCALE_BE_CLI=1` and never changes Tailscale login, Serve, or preferences.

Only online Self/Peer nodes with a stable node ID and a strict DNS name are
probed at `https://<dns>/grave/api/v1/summary`. Responses time out in three
seconds and are capped at 64 KiB; the app displays only the versioned
`gravedecay` summary contract. Up to 64 nodes are probed, eight at a time.
Validated plots and last-seen summaries are saved in app preferences and
restored as unreachable until discovery succeeds. Offline plots stay listed
and selected; **Forget plot** removes an unreachable entry. Graveyard opens
with all plots, and the header and menu-bar pickers select a destination.
See [Graveyard](../../docs/GRAVEYARD.md). Dashboard, T3, and Terminal actions are
constructed only from the selected DNS name and safe same-host single-slash
paths supplied by that contract. Graveyard selects remote graves only. A native
terminal is created only when the selected grave advertises `/term`.

Settings → **Local host** → **Start Local Host** is off by default. It starts
the bundled web host on `127.0.0.1:4712` (dashboard/PWA) and `:4714` (network).
The app owns its lifetime and holds an idle-sleep assertion while serving,
including with its window closed. The host refuses classic companion jobs or
occupied ports. The UI supplies manual Serve commands and opens `/grave/` on
this Mac's tailnet address for the same experience phones and PWAs use.
See [native hosting](../../docs/MACOS.md#native-app-hosting) for identity,
startup, supported services and migration.

When Tailscale is unavailable, Graveyard, Settings, and the menu bar offer
**Get Tailscale** or **Open Tailscale**. They only open the official app or
download page; sign-in and Tailscale configuration remain yours.

The direct-distribution macOS target deliberately is not App Sandbox enabled:
running the user-installed Tailscale CLI requires local process access. It
keeps its local plot inventory limited to identities, observation times and
sanitized summaries. Its opt-in host reuses the classic backend authorization
checks for private work and settings; it runs as the logged-in user and keeps
its state separate from the classic installation. Network requests remain HTTPS
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

## Bundled host runtime

The macOS build runs `scripts/prepare-native-host.py`: it copies the shared
backend/PWA/network files and downloads checksum-pinned CPython 3.13.15
`python-build-standalone` archives (20260901) for Intel and Apple Silicon.
Downloads are cached under `build/python-cache`; the installed app needs no
network download, Homebrew or developer tools to start its servers. The
runtime retains upstream licenses, with dependency notices in
`NativeHost/PYTHON-LICENSES.txt`. Runtime Mach-O files use the build signing
identity, or ad-hoc signing for unsigned builds.

CI starts the host from the mounted release DMG with a fresh temporary data
root. Tests cover the PWA, access checks, current summaries, port collisions
and parent-pipe cleanup using the bundled interpreter. Source-only tests use
`python3 -m unittest discover -s tests -p test_native_host.py`.

## Distribution notes

`make package-dmg` creates a mountable Universal 2 DMG at
`build/Gravedecay-macOS.dmg`; CI publishes that artifact, and tagged releases
attach it as `Gravedecay-macOS.dmg` with its SHA-256 in the release notes.
The packager preserves an existing app signature or adds an ad-hoc signature
so Launch at Login can identify the app. Ad-hoc signing does not notarize the
app or establish a verified publisher; macOS 15 Gatekeeper can block first launch:
open the app once, dismiss the warning, then allow it under **System
Settings → Privacy & Security → Open Anyway** (or clear the quarantine flag
with `xattr -d com.apple.quarantine /Applications/Gravedecay.app`). Set
`SIGNING='CODE_SIGNING_ALLOWED=YES CODE_SIGN_IDENTITY="Developer ID Application: …"'`
and `NOTARY_PROFILE` to a configured `notarytool` Keychain profile for a
signed, verified, notarized, and stapled release. Notarization refuses an
unsigned app; a Developer ID certificate/profile remains a release prerequisite.

If Launch at Login fails with “Operation not permitted” on an older unsigned or local
build, quit the app and give it a local ad-hoc signature:

```sh
codesign --sign - /Applications/Gravedecay.app
codesign --verify --strict /Applications/Gravedecay.app
```

Reopen the app and enable Launch at Login. This does not notarize the app or
replace Developer ID distribution signing. Repeat for an unsigned replacement
build; do not re-sign an already valid Developer ID-signed release.

- iOS personal: development signing / TestFlight ($99 dev account).
- iOS public (EU): AltStore PAL self-publishing — Apple notarization only,
  host the signed package ourselves.
