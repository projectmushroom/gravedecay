# Graveyard

A **plot** is one gravedecay appliance instance: your Mac, VM, or PC.
**Graveyard** is the overview and switcher for your plots. Projects, sessions,
credentials, and services remain on their original plot.

## Using it

- Web dashboard: open **Graveyard — your plots**, above the launcher. **All
  plots** shows the overview; the plot picker narrows it to one appliance.
  Dashboard, T3, Terminal, and Network links open that plot's advertised apps.
  CPU, memory, and disk percentages show the latest reachable summary.
- **Set up T3** opens the selected plot's dashboard at its pairing controls
  in web, macOS, and Omarchy. Create a fresh link there and add it as an
  environment in the official T3 app. Opening setup never creates a token;
  the destination's usual owner/workspace checks still apply. Older plots
  without the setup capability keep their existing Dashboard and T3 links.
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
peers do not fail doctor. It also checks that the local summary advertises T3
setup on the dashboard only when it advertises T3. Re-raise Linux or rerun the
macOS installer to apply
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

Gravedecay manages machine health, storage, services, updates, and T3 pairing.
Coding, approvals, and choosing where a new thread runs belong in T3. A separate
Graveyard activity feed or task dispatcher is deferred.

## Reusing T3

Use the official T3 app as the coding interface for all plots. Pair each plot
over its existing Tailscale HTTPS origin; each remains an independent T3
environment. GD's Tailscale node ID and T3's environment ID are different
identities. Discovery does not enroll a device in T3 or copy T3 credentials.

The [T3 v0.0.40 remote-access guide](https://github.com/pingdotgg/t3code/blob/v0.0.40/docs/user/remote-access.md)
documents multiple environments and optional **Settings → Connections → Load
balancing** on web/desktop. This chooses machines for new threads in grouped
projects; existing threads stay on their original machine. Mobile selection
is manual. GD does not implement another scheduler or synchronize these
client-local preferences.

Reuse T3's pairing links and app hand-off, its Connect activity publishing
(`grave t3 connect publish` on supported Linux appliances), and its optional
**Continue threads after restarts** setting. That setting resumes supported
threads once T3 starts again; it does not start the server or preserve every
terminal command. See [CLIENTS.md](CLIENTS.md) and the
[updating guide](https://github.com/pingdotgg/t3code/blob/v0.0.40/docs/user/updating.md).

T3's [connection catalog](https://github.com/pingdotgg/t3code/blob/v0.0.40/apps/web/src/state/environments.ts)
separates saved environments from live connection state. GD follows the same
principle with saved plots, explicit destinations, and unavailable states.
Its implementation depends on T3's TypeScript client runtime and auth; GD's
Python, Swift, and QML clients keep their small summary readers instead of
importing that stack. No upstream code is copied for this integration.

Keep GD as the service and Tailscale-route owner on raised appliances. T3's
own service installer, SSH launcher, and automatic Serve setup can create a
second server/profile or change routes. Use the existing GD pairing controls
and platform updater; enabling T3's remote service updater would require a
separate compatibility check for GD-managed services. Machine version and
connection diagnostics are a subsequent slice, not inferred from a T3 link.
