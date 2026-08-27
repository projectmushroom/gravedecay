# Gravedecay menu bar

macOS 15+ menu-bar fleet widget. It reads local `tailscale status --json`
about every 45 seconds, probes online Self and peers independently at
`/grave/api/v1/summary`, and retains only currently reachable validated v1
summaries in memory. It stores no inventory, credentials, or history and
opens no listener or control channel.

```sh
swift run --package-path clients/macos-menubar
swift test --package-path clients/macos-menubar
```

The app needs the Tailscale macOS app signed in to the intended tailnet.
