# Graveyard

A **plot** is one gravedecay appliance instance: your Mac, VM, or PC.
**Graveyard** is the overview and switcher for your plots. Projects, sessions,
credentials, and services remain on their original plot.

## Using it

- Web dashboard: open **Graveyard — your plots**, above the launcher. **All
  plots** shows the overview; the plot picker narrows it to one appliance.
  Dashboard, T3, Terminal, and Network links open that plot's advertised apps.
- macOS app: **Graveyard** starts with all plots. Select a card to inspect it;
  the header and menu-bar plot pickers select the destination. Terminal stays
  native; Dashboard and T3 open in the browser. This Mac, Work, and Network
  still describe the local Mac.
- Omarchy: open the Graveyard widget for the plot list, picker, and links.

Only appliances returning a validated v1 summary are remembered. Unreachable
plots remain listed after a refresh or client restart, with the last successful
observation time. Saved metrics are historical, never evidence that a plot is
online. An unavailable selected plot stays selected; it does not redirect to a
different machine. **Forget plot** removes an unreachable entry from this
client; discovery can add it again when it returns.

Discovery refreshes roughly every 45 seconds, probing at most 64 online
Tailscale nodes with up to eight requests at once. Requests have bounded
timeouts and a 64 KiB summary limit. Each inventory holds at most 128 plots.
Client devices must be able to reach a destination themselves to open its apps;
the web overview reports reachability from its hosting appliance.

## Storage and access

Saved inventory is local to each client, not synchronized: macOS uses app
preferences (`graveyardPlots`); Omarchy uses
`${XDG_STATE_HOME:-~/.local/state}/gravedecay/plots.json` in a private directory;
the web uses browser storage (`graveyard-plots-v1`) per dashboard origin.
Clearing that storage resets the remembered plots. The records contain node
IDs, names, DNS names, last-seen times, and sanitized status summaries, never
credentials, repository names, or transcripts.

The web's owner-only `GET /grave/api/graveyard` collects public summaries from
the appliance's current Tailscale peers. It does not forward the viewer's
identity, proxy private work, or execute remote commands. It runs separately
from `/api/state`, adds no listener, and serves uncached responses without CORS.
The browser hides inventory when the owner identity gate rejects a refresh.

Web discovery is supported by single-owner Linux and source-installed macOS
appliances. Portable and multi-user dashboard backends report `unsupported`;
they cannot safely discover peers or delegate the viewer's identity yet.
Native clients can still discover reachable plots through the existing summary
contract, subject to each destination's access checks.

`grave doctor`'s existing identity/maintenance check now also verifies the
Graveyard endpoint denies headerless inventory reads and that authenticated
responses have the expected shape, `no-store`, and no CORS header. Offline
peers do not fail doctor. Re-raise Linux or rerun the macOS installer to apply
the updated dashboard; rebuild the native app or update the Omarchy plugin for
their saved inventories.

## Current scope

This first release discovers one plot per Tailscale node at the standard
`https://<dns>/grave/api/v1/summary` endpoint, keyed by the stable node ID.
Renaming a node updates its record on successful discovery; re-enrolling it as
a different node creates a new record. Several portable instances on the same
host need explicit instance identities and endpoint registration in a later
release. Saved plots on another tailnet remain unreachable until that network
is accessible.

Shared work/attention feeds, choosing where a new task runs, and fleet
maintenance are subsequent steps. This release opens the existing apps of a
selected plot and leaves work execution on that plot.
