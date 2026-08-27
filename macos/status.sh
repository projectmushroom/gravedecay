#!/bin/sh
# Doctor-lite contract for the source-installed macOS companion ("doctor is
# the contract"): exit non-zero when an enforced invariant fails — agents
# loaded, loopback health, Serve mounts, keep-awake while serving, install
# drift, ntfy secret hygiene. --page additionally routes a failure through
# `grave notify --event doctor` (silent no-op until an ntfy topic exists);
# the periodic io.gravedecay.doctor agent runs exactly that.
set -u
ROOT=${GRAVEDECAY_MAC_ROOT:-"$HOME/Library/Application Support/Gravedecay"}
PYTHON=$(command -v python3 2>/dev/null || true)
PAGE=0
while [ $# -gt 0 ]; do case "$1" in
  --root) [ $# -ge 2 ] || { echo "--root needs a path" >&2; exit 2; }; shift; ROOT=$1;;
  --page) PAGE=1;;
  *) echo "usage: $0 [--root PATH] [--page]" >&2; exit 2;;
esac; shift; done
state="$ROOT/config/components"; rc=0
if [ ! -f "$ROOT/.gravedecay-macos" ] || [ ! -f "$state" ]; then echo "gravedecay macOS companion: not installed at $ROOT"; exit 0; fi
dash=$(sed -n 's/^dashboard=//p' "$state"); net=$(sed -n 's/^network=//p' "$state"); serve=$(sed -n 's/^serve=//p' "$state"); keep=$(sed -n 's/^keepawake=//p' "$state"); agentsmode=$(sed -n 's/^agents=//p' "$state"); uid=$(id -u)
case "$dash:$net:$serve" in 1:1:[01]|1:0:[01]|0:1:[01]) ;; *) echo "invalid component metadata" >&2; exit 2;; esac
# Pre-keepawake installs have no keepawake= line; a serving Mac is then
# expected to hold the assertion, so the missing agent fails loudly below.
[ -n "$keep" ] || keep=1
case "$keep" in 0|1) ;; *) echo "invalid component metadata" >&2; exit 2;; esac
# Pre-agents installs have no agents= line: observability-only.
[ -n "$agentsmode" ] || agentsmode=0
case "$agentsmode" in 0|1) ;; *) echo "invalid component metadata" >&2; exit 2;; esac
check(){ label=$1 port=$2 enabled=$3 path=${4:-/healthz}; [ "$enabled" = 1 ] || return 0
  if launchctl print "gui/$uid/$label" >/dev/null 2>&1; then echo "$label: loaded"; else echo "$label: not loaded"; rc=1; fi
  if curl -fsS "http://127.0.0.1:$port$path" >/dev/null 2>&1; then echo ":$port health: ok"; else echo ":$port health: failed"; rc=1; fi; }
check io.gravedecay.dashboard 4712 "$dash"; check io.gravedecay.network 4714 "$net"
# t3/ttyd have no /healthz; their answering root page is the liveness signal.
check io.gravedecay.t3 4711 "$agentsmode" /; check io.gravedecay.term 4713 "$agentsmode" /
if [ -n "$PYTHON" ] && [ -f "$ROOT/config/release.json" ]; then
  "$PYTHON" - "$ROOT/config/release.json" <<'PY' 2>/dev/null || true
import json, sys
v=json.load(open(sys.argv[1])); print("version: %s (checkout: %s, channel: %s)" % (v.get("current") or "development", v.get("checkout") or "unknown", v.get("channel") or "unknown"))
PY
  "$ROOT/scripts/grave" update-status 2>/dev/null || true
fi
for label in io.gravedecay.updater io.gravedecay.doctor; do
  if launchctl print "gui/$uid/$label" >/dev/null 2>&1; then echo "$label: loaded"; else echo "$label: not loaded"; rc=1; fi
done
ts=$(command -v tailscale 2>/dev/null || true); [ -n "$ts" ] || [ ! -x /Applications/Tailscale.app/Contents/MacOS/Tailscale ] || ts=/Applications/Tailscale.app/Contents/MacOS/Tailscale
if [ "$serve" = 0 ]; then
  echo "Serve: disabled (localhost-only)"
elif [ -n "$ts" ]; then
  out=$("$ts" serve status 2>&1) || { echo "Tailscale Serve: unavailable"; rc=1; out=; }
  [ -n "$out" ] || { echo "Tailscale Serve: empty status"; rc=1; }
  [ -z "$out" ] || echo "$out"
  [ "$dash" != 1 ] || printf '%s\n' "$out" | grep -q '/grave' || rc=1
  [ "$net" != 1 ] || printf '%s\n' "$out" | grep -q '/net' || rc=1
  # Agents mode publishes the whole origin layout: / -> 4711 and /term -> 4713.
  [ "$agentsmode" != 1 ] || printf '%s\n' "$out" | grep -q '127.0.0.1:4711' || rc=1
  [ "$agentsmode" != 1 ] || printf '%s\n' "$out" | grep -q '/term' || rc=1
else
  echo "Tailscale CLI: missing"; rc=1
fi
# A serving Mac that idle-sleeps is worse than no server — the paths go dark.
if [ "$serve" = 1 ] && [ "$keep" = 1 ]; then
  if launchctl print "gui/$uid/io.gravedecay.keepawake" >/dev/null 2>&1; then echo "io.gravedecay.keepawake: loaded"
  else echo "io.gravedecay.keepawake: not loaded (serving Mac may idle-sleep; rerun macos/install.sh)"; rc=1; fi
  if command -v pmset >/dev/null 2>&1; then
    if pmset -g assertions 2>/dev/null | grep -q caffeinate; then echo "sleep policy: caffeinate assertion held"
    else echo "sleep policy: no caffeinate assertion while serving"; rc=1; fi
  fi
elif [ "$serve" = 1 ]; then
  echo "keep-awake: opted out (--allow-sleep); tailnet paths go dark when this Mac sleeps"
fi
# Installs are copies; a managed checkout that moved on without a rerun serves
# stale code silently unless doctor-lite says so.
SRC="$ROOT/repos/gravedecay"
drift(){ name=$1 rel=$2 enabled=$3; [ "$enabled" = 1 ] || return 0; [ -d "$SRC/.git" ] && [ -f "$SRC/$rel" ] || return 0
  if cmp -s "$ROOT/scripts/$name" "$SRC/$rel"; then echo "$name: matches the managed checkout"
  else echo "$name: drifted from the managed checkout (rerun macos/install.sh)"; rc=1; fi; }
drift gravedecay.py dashboard/gravedecay.py "$dash"
drift gravenet.py dashboard/gravenet.py "$net"
NOTIFY_ENV="$ROOT/config/secrets/notify.env"
if [ -f "$NOTIFY_ENV" ]; then
  # GNU stat first: its -c errors cleanly on BSD, while BSD's -f "succeeds"
  # on GNU by dumping filesystem status instead of the permission bits.
  perm=$( { stat -c %a "$NOTIFY_ENV" || stat -f %Lp "$NOTIFY_ENV"; } 2>/dev/null )
  if [ "$perm" = 600 ]; then echo "ntfy secret: private (600)"; else echo "ntfy secret: must be chmod 600 (is ${perm:-unknown})"; rc=1; fi
  # Probe without publishing: the poll endpoint exercises the URL and auth
  # without paging the phone on every doctor run.
  if ( . "$NOTIFY_ENV" 2>/dev/null
       case "${NTFY_URL:-}" in http://*|https://*) ;; *) exit 1;; esac
       set -- -sf -m 10 -o /dev/null
       [ -z "${NTFY_TOKEN:-}" ] || set -- "$@" -H "Authorization: Bearer $NTFY_TOKEN"
       curl "$@" "$NTFY_URL/json?poll=1" ); then echo "ntfy channel: reachable"
  else echo "ntfy channel: unreachable or invalid NTFY_URL"; rc=1; fi
else
  echo "ntfy: not configured (optional — docs/NOTIFICATIONS.md)"
fi
if [ "$PAGE" = 1 ] && [ "$rc" != 0 ] && [ -x "$ROOT/scripts/grave" ]; then
  GRAVE_ROOT=$ROOT "$ROOT/scripts/grave" notify --event doctor --priority high \
    "macOS doctor-lite failing" "run macos/status.sh on $(hostname) for details" || true
fi
exit "$rc"
