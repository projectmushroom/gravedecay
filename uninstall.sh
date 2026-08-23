#!/usr/bin/env bash
# uninstall.sh — the inverse of install.sh / raise.sh.
#
#   ./uninstall.sh [--purge] [--yes] [--dry-run]
#
# The real teardown lives in `grave uninstall`: the installed CLI knows this
# box's GRAVE_ROOT, whether Docker is rootless, and which workspaces exist —
# none of which this script can recover from a repo checkout alone. So the
# normal path here is "find grave, hand off to it".
#
# This wrapper exists for the case where that hand-off is impossible: the CLI
# was already deleted, or /etc/gravedecay/grave.conf is missing/corrupt (grave
# exits at startup without it). Then it falls back to a self-contained removal
# of the fixed, config-independent surface — units, /etc entries, the CLIs —
# and tells you exactly what it could not reach.
#
# Scope, in both modes: removes what gravedecay installed, keeps $GRAVE_ROOT
# unless --purge, and never touches docker, tailscale's login, the toolchain or
# distro packages. See docs/UNINSTALL.md.
set -euo pipefail

PURGE=0; ASSUME_YES=0; DRY=0
ARGS=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --purge)      PURGE=1;      ARGS+=("$1") ;;
    --yes|-y)     ASSUME_YES=1; ARGS+=("$1") ;;
    --dry-run|-n) DRY=1;        ARGS+=("$1") ;;
    -h|--help)    sed -n '2,20p' "$0"; exit 0 ;;
    *) echo "unknown arg: $1"; echo "usage: ./uninstall.sh [--purge] [--yes] [--dry-run]"; exit 1 ;;
  esac
  shift
done

[[ $EUID -eq 0 ]] && { echo "Run as your normal user (sudo is used internally)."; exit 1; }

GRN=$'\e[32m'; YLW=$'\e[33m'; RED=$'\e[31m'; BLD=$'\e[1m'; RST=$'\e[0m'
step() { printf '\n%b🪦 %s%b\n' "$BLD" "$*" "$RST"; }
ok()   { printf '  %b✓%b %s\n' "$GRN" "$RST" "$*"; }
skip() { printf '  %b–%b %s\n' "$YLW" "$RST" "$*"; }

# ------------------------------------------------- preferred: hand off to grave ----
CONF="${GRAVE_CONF:-/etc/gravedecay/grave.conf}"
GRAVE_BIN=""
for c in "$(command -v grave 2>/dev/null || true)" /usr/local/bin/grave "$HOME/.local/bin/grave"; do
  [[ -n "$c" && -x "$c" ]] && { GRAVE_BIN="$c"; break; }
done
if [[ -n "$GRAVE_BIN" && -r "$CONF" ]]; then
  # ${ARGS[@]+...} so an empty array is not an unbound-variable error under set -u
  exec "$GRAVE_BIN" uninstall ${ARGS[@]+"${ARGS[@]}"}
fi

# ----------------------------------------------------------- fallback teardown ----
step "Fallback uninstall (the grave CLI can't run here)"
[[ -z "$GRAVE_BIN" ]] && skip "no grave CLI found — it was already removed"
[[ -r "$CONF" ]]      || skip "$CONF missing or unreadable — GRAVE_ROOT is unknown"
echo "  Removing the fixed platform surface only. Anything that depends on"
echo "  this box's config (containers, tailnet mounts, \$GRAVE_ROOT) is listed"
echo "  at the end for you to handle."

# GRAVE_ROOT is only needed to REPORT what's left; never guessed into an rm.
GRAVE_ROOT_GUESS=""
for g in /srv/dev "$HOME/gravedecay"; do
  [[ -d "$g" ]] && { GRAVE_ROOT_GUESS="$g"; break; }
done

if (( ! ASSUME_YES && ! DRY )); then
  [[ -t 0 ]] || { echo "needs a terminal to confirm — pass --yes"; exit 1; }
  printf 'Type %buninstall%b to continue: ' "$BLD" "$RST"
  read -r answer
  [[ "$answer" == "uninstall" ]] || { echo "Aborted — nothing was changed."; exit 1; }
fi
run() { if (( DRY )); then echo "  would: $*"; else "$@" || true; fi; }
# A dry run must not claim a result — `run` already printed the intent.
did() { (( DRY )) || ok "$*"; }

# Keep this list in sync with UNINSTALL_UNITS in bin/grave (tests/test_uninstall.py
# enforces it — a unit added to raise.sh must be removable by both paths).
UNITS=(
  gravedecay-backup.timer gravedecay-digest.timer
  gravedecay-auto-thaw.timer gravedecay-auto-thaw.service
  gravedecay-backup.service gravedecay-digest.service
  gravedecay-upgrade.service gravedecay-upgrade@.service
  gravedecay-gamewatch.service gravedecay-keepalive.service
  gravedecay-selfheal.service gravedecay-net.service
  gravedecay-term.service t3code.service
  gravedecay-gateway.service gravedecay-boundary.service gravedecay.service
  gravedecay-notify@.service
  gravedecay-t3@.service gravedecay-term@.service gravedecay-dashboard@.service
)
step "Services"
present=()
for u in "${UNITS[@]}"; do systemctl cat "$u" >/dev/null 2>&1 && present+=("$u"); done
# Template instances (multi-user workspaces) — enumerated from systemd itself,
# since without grave.conf we cannot read the workspace registry.
while IFS= read -r u; do [[ -n "$u" ]] && present+=("$u"); done < <(
  systemctl list-units --all --no-legend --plain 'gravedecay-*@*.service' 2>/dev/null | awk '{print $1}' || true)
if ((${#present[@]})); then
  run sudo systemctl stop "${present[@]}"
  run sudo systemctl disable "${present[@]}"
  did "stopped and disabled ${#present[@]} unit(s)"
else
  skip "no gravedecay units installed"
fi
files=()
for u in "${UNITS[@]}"; do [[ -e "/etc/systemd/system/$u" ]] && files+=("/etc/systemd/system/$u"); done
[[ -e /etc/systemd/system/tailscaled.service.d/gravedecay-localapi.conf ]] \
  && files+=(/etc/systemd/system/tailscaled.service.d/gravedecay-localapi.conf)
[[ -e /etc/systemd/system/gravedecay.service.d/multiuser-boundary.conf ]] \
  && files+=(/etc/systemd/system/gravedecay.service.d/multiuser-boundary.conf)
if ((${#files[@]})); then run sudo rm -f "${files[@]}"; did "removed ${#files[@]} unit file(s)"; fi
(( DRY )) || sudo rmdir /etc/systemd/system/tailscaled.service.d 2>/dev/null || true
(( DRY )) || sudo rmdir /etc/systemd/system/gravedecay.service.d 2>/dev/null || true
run sudo systemctl daemon-reload
(( DRY )) || sudo systemctl reset-failed 2>/dev/null || true

step "Tailnet"
if command -v tailscale >/dev/null 2>&1 && tailscale status --peers=false >/dev/null 2>&1; then
  for p in /grave /term /net; do run tailscale serve --https=443 --set-path="$p" off; done
  run tailscale serve --https=443 off
  did "gravedecay serve mounts cleared (node still logged in)"
else
  skip "tailscale not available/logged in — serve config untouched"
fi
if command -v nft >/dev/null 2>&1; then
  run sudo nft delete table inet gravedecay
  did "multi-user loopback boundary removed"
fi
# After the serve teardown, never before it: the restart drops the LocalAPI
# drop-in removed above and leaves the daemon warming up, and `tailscale
# status` fails for that whole window — which this script reads as "not logged
# in", skipping the mounts entirely.
systemctl is-active --quiet tailscaled 2>/dev/null && run sudo systemctl restart tailscaled

step "System configuration"
for f in /etc/sudoers.d/50-gravedecay /etc/sudoers.d/zz-gravedecay; do run sudo rm -f "$f"; done
did "sudoers drop-ins removed"
[[ -d /etc/gravedecay ]]         && { run sudo rm -rf /etc/gravedecay;         did "/etc/gravedecay removed"; }
[[ -d /usr/libexec/gravedecay ]] && { run sudo rm -rf /usr/libexec/gravedecay; did "/usr/libexec/gravedecay removed"; }
# raise.sh masks these on always-on boxes (profiles/steam-machine.sh); without
# grave.conf we can't read CHECK_SLEEP_MASKED, so only unmask what IS masked.
masked=()
for t in sleep.target suspend.target hibernate.target hybrid-sleep.target; do
  [[ "$(systemctl is-enabled "$t" 2>/dev/null)" == masked ]] && masked+=("$t")
done
((${#masked[@]})) && { run sudo systemctl unmask "${masked[@]}"; did "unmasked: ${masked[*]}"; }

step "CLI"
for c in /usr/local/bin/grave /usr/local/bin/grave-workspaces; do
  [[ -e "$c" ]] && { run sudo rm -f "$c"; did "$c removed"; }
done
for c in "$HOME/.local/bin/grave" "$HOME/.local/bin/grave-workspaces"; do
  [[ -e "$c" ]] && { run rm -f "$c"; did "$c removed"; }
done

step "Not handled by the fallback path"
echo "  Without the CLI's view of this box, these need you:"
if [[ -n "$GRAVE_ROOT_GUESS" ]]; then
  if (( PURGE )); then
    echo "  • ${RED}--purge is NOT honored here${RST} — delete deliberately: sudo rm -rf $GRAVE_ROOT_GUESS"
  else
    echo "  • $GRAVE_ROOT_GUESS kept (repos, agents, secrets, backups)"
  fi
  echo "  • containers/volumes: docker compose --project-directory $GRAVE_ROOT_GUESS/docker/core down"
else
  echo "  • \$GRAVE_ROOT was not found at /srv/dev or ~/gravedecay — locate and remove it yourself"
fi
echo "  • docker network: docker network rm devnet"
echo "  • agent hooks: remove the grave-agent-notify entries from ~/.claude/settings.json and ~/.codex/config.toml"
command -v tailscale >/dev/null 2>&1 && echo "  • this node is still on the tailnet — 'sudo tailscale logout' to leave"
echo ""
(( DRY )) && echo "🪦 Dry run only — nothing was changed." || echo "🪦 Platform removed (fallback path)."
