# Graveyard

A **plot** is one gravedecay appliance instance: your Mac, VM, or PC.
**Graveyard** is the overview and switcher for your plots. Projects, sessions,
credentials, and services remain on their original plot.

## Using it

- Web dashboard: the compact **Graveyard / Switch** row above the launcher
  identifies the machine currently being managed and counts remembered graves.
  Tap it to open a scrollable switcher; it starts closed on every page load.
  Search by name or DNS address. **Open grave** opens that dashboard; **T3**
  jumps directly to its advertised T3 app. The current machine appears first
  with **This grave** when its discovered DNS matches the dashboard hostname.
  Connection state and reported problems stay visible. **Details** reveals the
  full name/address, T3 service state, CPU/memory/disk, sessions, Terminal,
  Network, and pairing setup. Refresh preserves the search and expanded details.
  Close, Escape, or tapping outside returns to the launcher. Merely searching
  or inspecting a grave never changes the machine being managed.
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

Mobile previews with fixture data: [collapsed switcher](../assets/graveyard-closed.png)
and [open Graveyard](../assets/graveyard-switcher.png).

Plots keep their discovered name, platform icon, and an accent derived from
their normalized DNS address across refreshes and client restarts. The same
address gets the same accent in web, macOS, and Omarchy. The palette is finite:
colours can repeat, so the web switcher's Details always includes the full
name and DNS address (native clients show these in their overview). Renaming the
DNS address may change its colour; native clients' saved selection still uses the node ID.
The destination web dashboard labels the machine being managed and uses its
own hostname/address for pairing controls, never an identity supplied by a
link from another plot.

Connection text distinguishes **Checking connection**, **Dashboard reachable**,
and **Unreachable — check Tailscale or dashboard**. A reachable dashboard can
separately report **T3 stopped**, **starting**, **service failed**, **service
running**, **not configured**, or **status unknown**. These are supervisor
observations, not proof of a usable or paired T3 session. An unreachable plot
does not prove the whole machine or tailnet is down. Refreshing plots disables
their app/setup actions until discovery completes; it never chooses another
machine. Old summaries and unsupported supervisors explicitly show unknown.

Only appliances returning a validated v1 summary are remembered. Unreachable
plots remain listed after a refresh or client restart, with the last successful
observation time. Saved metrics are historical, never evidence that a plot is
online. An unavailable plot stays listed; it does not redirect to a
different machine. **Forget plot**, under web Details, removes an unreachable
entry from this client; discovery can add it again when it returns.

Discovery refreshes roughly every 45 seconds, probing at most 64 online
Tailscale nodes with up to eight requests at once. Requests have bounded
timeouts and a 64 KiB summary limit. Each inventory holds at most 128 plots.
Client devices must be able to reach a destination themselves to open its apps;
the web overview reports reachability from its hosting appliance.

## Installed PWA navigation

Install from `/grave/`. Its manifest covers `/` on that **one origin**, so
local Dashboard, T3, Terminal, and Network links use the current app view.
Graveyard uses ordinary `target="_self"` links, with no popup or iframe.
Links to another origin carry **↗**; the installed switcher also explains how
to return. On iPhone/iPad, another grave's hostname is outside the installed
app's scope, so iOS opens a Safari browser sheet. Close that sheet to return
to the original Graveyard. Other browsers may use a tab or another external
navigation surface. This is browser behavior, not a second installed PWA.
See [Apple's web app navigation guidance](https://developer.apple.com/videos/play/wwdc2023/10120/)
and the [manifest scope reference](https://developer.mozilla.org/en-US/docs/Web/Progressive_web_apps/Manifest/Reference/scope).

One installation remains a launcher for all reachable graves, but cannot
make different hostnames share its standalone scope on iOS. Each destination
keeps its own authentication and pairing. We do not proxy private apps or
embed remote dashboards to disguise that boundary.

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
setup on the dashboard only when it advertises T3, and verifies the installed
PWA's root scope, standalone display, dashboard identity and launch URL.
Re-raise Linux or rerun the macOS installer to apply the updated dashboard;
rebuild the native app or update the Omarchy plugin for their saved inventories.

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
T3 HTTP/pairing diagnostics are a subsequent slice, not inferred from a T3
link or the new supervisor-state field. Doctor validates that field's enum
alongside the local setup capability.

### Operating-system identity

Grave names in the dashboard switcher, grave cards, settings/update headings,
Omarchy grave list and native Apple grave pickers use bundled OS logos. The
logo describes the running OS: an Omarchy MacBook shows Arch; macOS shows Apple.
The product skull and tool icons (for example Terminal) retain their own meaning.

The optional v1 summary fields `node.os_icon` and `node.os_name` carry the
server's identity; `/api/state` includes the same fields at the top level.
Linux reads `ID` and `ID_LIKE` from `/etc/os-release`, falling back to
`/usr/lib/os-release`. The distro ID wins, then its first recognized family;
Omarchy, CachyOS and SteamOS use Arch. Missing/unknown Linux metadata uses Tux.
macOS and portable workspaces report Apple and Docker respectively without
using the container image's distro to imply a host OS. Saved graves retain their
last known identity while unreachable. Older summaries still work using their
platform's generic logo.

Clients allow only bundled icon keys, never peer-supplied image URLs or SVG.
The artwork is a pinned subset of Font Logos; sources, license and regeneration
instructions are in `assets/os-logos/README.md`. The web sprite is embedded in
the delivered page, so an open dashboard needs no extra requests to display it.
`grave doctor` verifies the local summary's OS identity and the installed sprite
as part of the dashboard auth/summary check. Native clients bundle the same
vector artwork; they require a client update to display it.
