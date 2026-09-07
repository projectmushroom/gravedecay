# Gravedecay for Omarchy

A native Omarchy 4/Quattro bar widget. It requires Omarchy's Quickshell plugin
support, `tailscale`, and `curl`.

From this directory, the supported local install is:

```sh
./install-local.sh
```

The upstream `omarchy plugin add` command accepts a Git URL, not this nested
payload path. The helper validates the manifest, copies only the plugin payload
to `~/.config/omarchy/plugins/projectmushroom.gravedecay/`, rescans, and
enables it. It refuses an existing directory. To deliberately atomically
replace an existing copy of this same ID:

```sh
./install-local.sh --update
```

Remove it with `omarchy plugin remove projectmushroom.gravedecay`.

Every 30–60 seconds (and whenever its popup opens), the widget runs
`tailscale status --json`, takes `Self` and online peers with DNS names and
stable IDs, then uses bounded argv-form `curl` requests to
`https://<dns>/grave/api/v1/summary`. Only responses with
`product: "gravedecay"` and `api_version: 1` are displayed. Up to 64 nodes are
probed, eight at a time. Validated plots, sanitized summaries, selection, and
last-seen times are saved under
`${XDG_STATE_HOME:-~/.local/state}/gravedecay/plots.json`. Offline plots remain
visible after restart and never cause selection to jump to another plot.
**Forget plot** removes an unreachable entry. No tokens, credentials, private
work, or remote controls are stored. See [Graveyard](../../docs/GRAVEYARD.md).

For local checks without changing Omarchy configuration:

```sh
omarchy plugin validate .
node tests/model.test.js
```

Do not launch a separate Quickshell process: this is injected into the running
Omarchy bar. On an Omarchy test desktop, install it and use left click to open
the switcher; right/middle click refreshes it.
