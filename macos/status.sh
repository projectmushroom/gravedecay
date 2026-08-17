#!/bin/sh
# Read-only doctor-lite for the source-installed macOS companion.
set -u
ROOT=${GRAVEDECAY_MAC_ROOT:-"$HOME/Library/Application Support/Gravedecay"}
if [ "${1:-}" = "--root" ]; then [ $# -ge 2 ] || { echo "--root needs a path" >&2; exit 2; }; ROOT=$2; shift 2; fi
[ $# -eq 0 ] || { echo "usage: $0 [--root PATH]" >&2; exit 2; }
state="$ROOT/config/components"; rc=0
if [ ! -f "$ROOT/.gravedecay-macos" ] || [ ! -f "$state" ]; then echo "gravedecay macOS companion: not installed at $ROOT"; exit 0; fi
dash=$(sed -n 's/^dashboard=//p' "$state"); net=$(sed -n 's/^network=//p' "$state"); serve=$(sed -n 's/^serve=//p' "$state"); uid=$(id -u)
case "$dash:$net:$serve" in [01]:[01]:[01]) ;; *) echo "invalid component metadata" >&2; exit 2;; esac
check(){ label=$1 port=$2 enabled=$3; [ "$enabled" = 1 ] || return 0
  if launchctl print "gui/$uid/$label" >/dev/null 2>&1; then echo "$label: loaded"; else echo "$label: not loaded"; rc=1; fi
  if curl -fsS "http://127.0.0.1:$port/healthz" >/dev/null 2>&1; then echo ":$port health: ok"; else echo ":$port health: failed"; rc=1; fi; }
check io.gravedecay.dashboard 4712 "$dash"; check io.gravedecay.network 4714 "$net"
ts=$(command -v tailscale 2>/dev/null || true); [ -n "$ts" ] || [ ! -x /Applications/Tailscale.app/Contents/MacOS/Tailscale ] || ts=/Applications/Tailscale.app/Contents/MacOS/Tailscale
if [ "$serve" = 0 ]; then
  echo "Serve: disabled (localhost-only)"
elif [ -n "$ts" ]; then
  out=$("$ts" serve status 2>&1) || { echo "Tailscale Serve: unavailable"; rc=1; out=; }
  [ -n "$out" ] || { echo "Tailscale Serve: empty status"; rc=1; }
  [ -z "$out" ] || echo "$out"
  [ "$dash" != 1 ] || printf '%s\n' "$out" | grep -q '/grave' || rc=1
  [ "$net" != 1 ] || printf '%s\n' "$out" | grep -q '/net' || rc=1
else
  echo "Tailscale CLI: missing"; rc=1
fi
exit "$rc"
