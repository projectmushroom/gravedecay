#!/bin/sh
# User-scoped macOS companion installer.  It deliberately does not raise the
# Linux appliance or install/manage T3, Docker, ttyd, SSH, firewall, or tailscale.
set -eu
HERE=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
ROOT=${GRAVEDECAY_MAC_ROOT:-"$HOME/Library/Application Support/Gravedecay"}
WANT_DASH=1 WANT_NET=1 SERVE=1 DRY=0
usage(){ echo "usage: $0 [--dashboard-only|--network-only] [--no-serve] [--root PATH] [--dry-run]"; }
while [ $# -gt 0 ]; do case "$1" in
  --dashboard-only) WANT_NET=0;; --network-only) WANT_DASH=0;; --no-serve) SERVE=0;;
  --root) [ $# -ge 2 ] || { echo "--root needs a path" >&2; exit 2; }; shift; ROOT=$1;; --dry-run) DRY=1;; -h|--help) usage; exit 0;; *) usage >&2; exit 2;; esac; shift; done
[ "$WANT_DASH" = 1 ] || [ "$WANT_NET" = 1 ] || { echo "choose only one component mode" >&2; exit 2; }
[ "$(uname -s)" = Darwin ] || { echo "macOS installer requires Darwin" >&2; exit 1; }
PYTHON=$(command -v python3 || true)
[ -n "$PYTHON" ] || { echo "python3 is required" >&2; exit 1; }
ROOT=$("$PYTHON" -c 'import os,sys; print(os.path.realpath(sys.argv[1]))' "$ROOT")
HOME_CANON=$("$PYTHON" -c 'import os; print(os.path.realpath(os.environ["HOME"]))')
case "$ROOT" in "$HOME_CANON"/*) ;; *) echo "--root must be an absolute descendant of $HOME" >&2; exit 2;; esac
case "$ROOT" in *[\&\|\<\>]* ) echo "--root must not contain XML/sed-sensitive characters" >&2; exit 2;; esac
TAILSCALE=$(command -v tailscale || true)
[ -n "$TAILSCALE" ] || [ ! -x /Applications/Tailscale.app/Contents/MacOS/Tailscale ] || TAILSCALE=/Applications/Tailscale.app/Contents/MacOS/Tailscale
[ "$SERVE" = 0 ] || [ -n "$TAILSCALE" ] || { echo "Tailscale CLI is required to publish Serve paths" >&2; exit 1; }
ALLOWED_USERS=""
if [ "$SERVE" = 1 ]; then
  # Serve forwards an authenticated Tailscale login.  Bind private project and
  # integration data to the identity currently signed in on this Mac; do not
  # guess from a hostname or permit an empty allow-list.  The JSON mapping is
  # deliberately Self.UserID -> User["<id>"].LoginName (the same value Serve
  # puts in Tailscale-User-Login), and no status JSON is logged.
  TAILSCALE_LOGIN=$("$TAILSCALE" status --json 2>/dev/null | "$PYTHON" -c '
import json, sys
try:
    status = json.load(sys.stdin)
    user_id = (status.get("Self") or {}).get("UserID")
    user = (status.get("User") or {}).get(str(user_id))
    login = (user or {}).get("LoginName")
    if not isinstance(login, str):
        raise ValueError("missing LoginName")
    print(login.strip())
except Exception:
    raise SystemExit(1)
') || { echo "Could not determine the signed-in local Tailscale identity; refusing to enable Serve" >&2; exit 1; }
  case "$TAILSCALE_LOGIN" in
    ""|*[!A-Za-z0-9._+@-]*) echo "Invalid local Tailscale login; refusing to enable Serve" >&2; exit 1;;
  esac
  ALLOWED_USERS=$TAILSCALE_LOGIN
fi
UID_NOW=$(id -u); AGENTS="$HOME/Library/LaunchAgents"
run(){ if [ "$DRY" = 1 ]; then printf 'dry-run: '; printf '%s ' "$@"; printf '\n'; else "$@"; fi; }
APPS=""; [ "$WANT_NET" = 0 ] || APPS="📡 Network=/net/"
render(){ template=$1; target=$2; if [ "$DRY" = 1 ]; then echo "dry-run: render $template -> $target"; else sed "s|@PYTHON@|$PYTHON|g;s|@ROOT@|$ROOT|g;s|@APPS@|$APPS|g;s|@ALLOWED_USERS@|$ALLOWED_USERS|g" "$template" > "$target"; plutil -lint "$target" >/dev/null; fi; }
unload(){ label=$1; plist="$AGENTS/$label.plist"; [ -e "$plist" ] && run launchctl bootout "gui/$UID_NOW" "$plist" || true; run rm -f "$plist"; }
serve_off(){ path=$1; [ -z "$TAILSCALE" ] || run "$TAILSCALE" serve --https=443 --set-path="$path" off; }
[ "$DRY" = 1 ] || { run mkdir -p "$ROOT/scripts" "$ROOT/web/net" "$ROOT/logs" "$ROOT/config" "$AGENTS"; : > "$ROOT/.gravedecay-macos"; }
if [ "$WANT_DASH" = 1 ]; then
  run cp "$HERE/../dashboard/gravedecay.py" "$ROOT/scripts/gravedecay.py"
  run cp "$HERE/../assets/gravedecay.png" "$ROOT/config/gravedecay.png"
  render "$HERE/LaunchAgents/io.gravedecay.dashboard.plist.tmpl" "$AGENTS/io.gravedecay.dashboard.plist"
  run launchctl bootout "gui/$UID_NOW" "$AGENTS/io.gravedecay.dashboard.plist" || true
  run launchctl bootstrap "gui/$UID_NOW" "$AGENTS/io.gravedecay.dashboard.plist"
else unload io.gravedecay.dashboard; serve_off /grave; fi
if [ "$WANT_NET" = 1 ]; then
  run cp "$HERE/../dashboard/gravenet.py" "$ROOT/scripts/gravenet.py"; run cp "$HERE/../web/net/index.html" "$ROOT/web/net/index.html"
  render "$HERE/LaunchAgents/io.gravedecay.network.plist.tmpl" "$AGENTS/io.gravedecay.network.plist"
  run launchctl bootout "gui/$UID_NOW" "$AGENTS/io.gravedecay.network.plist" || true
  run launchctl bootstrap "gui/$UID_NOW" "$AGENTS/io.gravedecay.network.plist"
else unload io.gravedecay.network; serve_off /net; fi
health(){ port=$1; i=0; while [ "$i" -lt 20 ]; do curl -fsS "http://127.0.0.1:$port/healthz" >/dev/null && return 0; i=$((i+1)); sleep 1; done; return 1; }
if [ "$DRY" = 0 ]; then [ "$WANT_DASH" = 0 ] || health 4712; [ "$WANT_NET" = 0 ] || health 4714; fi
if [ "$SERVE" = 1 ]; then
  [ "$WANT_DASH" = 1 ] && run "$TAILSCALE" serve --bg --https=443 --set-path=/grave http://127.0.0.1:4712
  [ "$WANT_NET" = 1 ] && run "$TAILSCALE" serve --bg --https=443 --set-path=/net http://127.0.0.1:4714
else
  [ "$WANT_DASH" = 0 ] || serve_off /grave
  [ "$WANT_NET" = 0 ] || serve_off /net
fi
[ "$DRY" = 1 ] || printf 'dashboard=%s\nnetwork=%s\nserve=%s\n' "$WANT_DASH" "$WANT_NET" "$SERVE" > "$ROOT/config/components"
echo "Installed macOS dashboard/network companion at $ROOT. T3 is intentionally unmanaged."
