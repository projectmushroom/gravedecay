#!/bin/sh
set -eu
ROOT=${GRAVEDECAY_MAC_ROOT:-"$HOME/Library/Application Support/Gravedecay"}; PURGE=0 DRY=0
while [ $# -gt 0 ]; do case "$1" in --purge) PURGE=1;; --dry-run) DRY=1;; --root) [ $# -ge 2 ] || { echo "--root needs a path" >&2; exit 2; }; shift; ROOT=$1;; -h|--help) echo "usage: $0 [--root PATH] [--purge] [--dry-run]"; exit 0;; *) exit 2;; esac; shift; done
case "$ROOT" in "$HOME"/*) ;; *) echo "--root must be an absolute descendant of $HOME" >&2; exit 2;; esac
if [ "$PURGE" = 1 ]; then
  ROOT_CANON=$(CDPATH= cd -- "$ROOT" && pwd -P) || { echo "refusing purge: root does not exist" >&2; exit 2; }
  HOME_CANON=$(CDPATH= cd -- "$HOME" && pwd -P)
  case "$ROOT_CANON" in "$HOME_CANON"/*) ;; *) echo "refusing unsafe purge root" >&2; exit 2;; esac
  [ -f "$ROOT_CANON/.gravedecay-macos" ] || { echo "refusing purge: not a marked Gravedecay macOS root" >&2; exit 2; }
  ROOT=$ROOT_CANON
fi
TS=$(command -v tailscale || true); [ -n "$TS" ] || [ ! -x /Applications/Tailscale.app/Contents/MacOS/Tailscale ] || TS=/Applications/Tailscale.app/Contents/MacOS/Tailscale
run(){ if [ "$DRY" = 1 ]; then printf 'dry-run: '; printf '%s ' "$@"; printf '\n'; else "$@"; fi; }
uid=$(id -u); agents="$HOME/Library/LaunchAgents"
for label in io.gravedecay.dashboard io.gravedecay.network; do plist="$agents/$label.plist"; [ -e "$plist" ] && run launchctl bootout "gui/$uid" "$plist" || true; run rm -f "$plist"; done
[ -z "$TS" ] || { run "$TS" serve --https=443 --set-path=/grave off; run "$TS" serve --https=443 --set-path=/net off; }
if [ "$PURGE" = 1 ]; then
  run rm -rf "$ROOT"
fi
if [ "$PURGE" = 1 ]; then data_note="data was purged by request"; else data_note="user data was kept"; fi
echo "Removed only Gravedecay macOS LaunchAgents and /grave,/net Serve mounts; Tailscale was kept; $data_note."
