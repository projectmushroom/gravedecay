#!/bin/sh
# User-scoped macOS companion installer.  It deliberately does not raise the
# Linux appliance or install/manage T3, Docker, ttyd, SSH, firewall, or tailscale.
set -eu
HERE=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
ROOT=${GRAVEDECAY_MAC_ROOT:-"$HOME/Library/Application Support/Gravedecay"}
WANT_DASH=1 WANT_NET=1 WANT_AGENTS=0 SERVE=1 KEEPAWAKE=1 DRY=0 NO_BOOTSTRAP=0 EXPLICIT_COMPONENT=0 EXPLICIT_SERVE=0 EXPLICIT_KEEPAWAKE=0
ORIGIN=https://github.com/projectmushroom/gravedecay.git
usage(){ echo "usage: $0 [--dashboard-only|--network-only] [--agents] [--no-serve] [--allow-sleep] [--root PATH] [--dry-run]"; }
while [ $# -gt 0 ]; do case "$1" in
  --dashboard-only) WANT_NET=0; EXPLICIT_COMPONENT=1;; --network-only) WANT_DASH=0; EXPLICIT_COMPONENT=1;; --no-serve) SERVE=0; EXPLICIT_SERVE=1;;
  # Deliberately NOT sticky: the agents layer is a per-run opt-in, and a rerun
  # without --agents converges the Mac back to observability-only.
  --agents) WANT_AGENTS=1;;
  --allow-sleep) KEEPAWAKE=0; EXPLICIT_KEEPAWAKE=1;;
  --root) [ $# -ge 2 ] || { echo "--root needs a path" >&2; exit 2; }; shift; ROOT=$1;; --dry-run) DRY=1;;
  --no-bootstrap) NO_BOOTSTRAP=1;; -h|--help) usage; exit 0;; *) usage >&2; exit 2;; esac; shift; done
[ "$WANT_DASH" = 1 ] || [ "$WANT_NET" = 1 ] || { echo "choose only one component mode" >&2; exit 2; }
# Without the dashboard there is no pairing UI anywhere — an unpairable T3
# origin whose only surface is a shell is a misconfiguration, not a mode.
[ "$WANT_AGENTS" = 0 ] || [ "$WANT_DASH" = 1 ] || { echo "--agents requires the dashboard component" >&2; exit 2; }
[ "$(uname -s)" = Darwin ] || { echo "macOS installer requires Darwin" >&2; exit 1; }
PYTHON=$(command -v python3 || true)
[ -n "$PYTHON" ] || { echo "python3 is required" >&2; exit 1; }
ROOT=$("$PYTHON" -c 'import os,sys; print(os.path.realpath(sys.argv[1]))' "$ROOT")
HOME_CANON=$("$PYTHON" -c 'import os; print(os.path.realpath(os.environ["HOME"]))')
case "$ROOT" in "$HOME_CANON"/*) ;; *) echo "--root must be an absolute descendant of $HOME" >&2; exit 2;; esac
case "$ROOT" in *[\&\|\<\>]* ) echo "--root must not contain XML/sed-sensitive characters" >&2; exit 2;; esac
SOURCE="$ROOT/repos/gravedecay"; UPDATER=${GRAVEDECAY_UPDATER:-0}; BREW_TAG=${GRAVEDECAY_MAC_BREW_TAG:-}; BREW_BACKUP=""
[ -z "$BREW_TAG" ] || "$PYTHON" -c 'import re,sys; raise SystemExit(not bool(re.fullmatch(r"v\d+\.\d+\.\d+",sys.argv[1])))' "$BREW_TAG" || { echo "invalid Homebrew release tag" >&2; exit 2; }
OLD_AGENTS=0
if [ -f "$ROOT/config/components" ]; then
  OLD_DASH=$(sed -n 's/^dashboard=//p' "$ROOT/config/components"); OLD_NET=$(sed -n 's/^network=//p' "$ROOT/config/components"); OLD_SERVE=$(sed -n 's/^serve=//p' "$ROOT/config/components")
  OLD_KEEP=$(sed -n 's/^keepawake=//p' "$ROOT/config/components"); OLD_AGENTS=$(sed -n 's/^agents=//p' "$ROOT/config/components")
  case "$OLD_DASH:$OLD_NET:$OLD_SERVE" in 1:1:[01]|1:0:[01]|0:1:[01]) ;; *) echo "invalid component metadata" >&2; exit 2;; esac
  case "$OLD_KEEP" in ''|0|1) ;; *) echo "invalid component metadata" >&2; exit 2;; esac
  case "$OLD_AGENTS" in '') OLD_AGENTS=0;; 0|1) ;; *) echo "invalid component metadata" >&2; exit 2;; esac
  [ "$EXPLICIT_COMPONENT" = 1 ] || { WANT_DASH=$OLD_DASH; WANT_NET=$OLD_NET; }
  [ "$EXPLICIT_SERVE" = 1 ] || SERVE=$OLD_SERVE
  [ "$EXPLICIT_KEEPAWAKE" = 1 ] || [ -z "$OLD_KEEP" ] || KEEPAWAKE=$OLD_KEEP
fi
case "$WANT_DASH:$WANT_NET:$SERVE" in 1:1:[01]|1:0:[01]|0:1:[01]) ;; *) echo "invalid component metadata" >&2; exit 2;; esac
run_install(){ set -- "$@" --root "$ROOT"; [ "$WANT_DASH" = 1 ] && [ "$WANT_NET" = 0 ] && set -- "$@" --dashboard-only; [ "$WANT_NET" = 1 ] && [ "$WANT_DASH" = 0 ] && set -- "$@" --network-only; [ "$SERVE" = 0 ] && set -- "$@" --no-serve; [ "$KEEPAWAKE" = 0 ] && set -- "$@" --allow-sleep; [ "$WANT_AGENTS" = 0 ] || set -- "$@" --agents; "$SOURCE/macos/install.sh" "$@"; }
source_install(){ if [ "${1:-0}" = 0 ]; then run_install; else GRAVEDECAY_MAC_BREW_TAG= run_install; fi; }
if [ "$NO_BOOTSTRAP" = 0 ] && [ -n "$BREW_TAG" ] && [ -d "$SOURCE/.git" ] && [ "$DRY" = 0 ]; then
  [ "$(git -C "$SOURCE" remote get-url origin 2>/dev/null || true)" = "$ORIGIN" ] && [ -z "$(git -C "$SOURCE" status --porcelain)" ] || { echo "managed source is untrusted or dirty" >&2; exit 1; }
  [ "$(git -C "$SOURCE" describe --tags --exact-match HEAD 2>/dev/null || true)" = "$BREW_TAG" ] || {
    mkdir -p "$ROOT/staging"; BOOT="$ROOT/staging/homebrew-$BREW_TAG-$$"; BACKUP="$ROOT/staging/previous-$$"
    git clone --quiet "$ORIGIN" "$BOOT" && git -C "$BOOT" checkout -q "$BREW_TAG" || { rm -rf "$BOOT"; echo "could not stage Homebrew release $BREW_TAG" >&2; exit 1; }
    mv "$SOURCE" "$BACKUP" && mv "$BOOT" "$SOURCE" || { [ ! -d "$BACKUP" ] || [ -d "$SOURCE" ] || mv "$BACKUP" "$SOURCE"; echo "could not adopt Homebrew release $BREW_TAG" >&2; exit 1; }
    BREW_BACKUP=$BACKUP
  }
fi
if [ "$NO_BOOTSTRAP" = 0 ] && [ ! -d "$SOURCE/.git" ] && [ "$DRY" = 0 ]; then
  mkdir -p "$ROOT/repos" "$ROOT/staging"; BOOT="$ROOT/staging/bootstrap-$$"
  CURRENT=$(CDPATH= cd -- "$HERE/.." && pwd)
  if git -C "$CURRENT" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    [ "$(git -C "$CURRENT" remote get-url origin 2>/dev/null || true)" = "$ORIGIN" ] || { echo "current source origin is not trusted" >&2; exit 1; }
    [ -z "$(git -C "$CURRENT" status --porcelain)" ] || { echo "current source has local changes; refusing bootstrap" >&2; exit 1; }
    git clone --quiet "$CURRENT" "$BOOT" && git -C "$BOOT" remote set-url origin "$ORIGIN" || { echo "could not bootstrap managed source" >&2; exit 1; }
  else
    git clone --quiet "$ORIGIN" "$BOOT" || { echo "could not bootstrap managed source" >&2; exit 1; }
    BOOT_TAG=$BREW_TAG
    [ -n "$BOOT_TAG" ] || BOOT_TAG=$(git -C "$BOOT" tag -l 'v*' | "$PYTHON" -c 'import re,sys; x=[s.strip() for s in sys.stdin if re.fullmatch(r"v\d+\.\d+\.\d+",s.strip())]; print(max(x,key=lambda v:tuple(map(int,v[1:].split(".")))) if x else "")')
    [ -z "$BOOT_TAG" ] || git -C "$BOOT" checkout -q "$BOOT_TAG"
  fi
  mv "$BOOT" "$SOURCE"
  run_install --no-bootstrap; exit $?
fi
[ "$NO_BOOTSTRAP" = 1 ] || [ "$DRY" = 1 ] || [ ! -d "$SOURCE/.git" ] || {
  CURRENT=$(CDPATH= cd -- "$HERE/.." && pwd); [ "$CURRENT" = "$SOURCE" ] || {
    [ "$(git -C "$SOURCE" remote get-url origin 2>/dev/null || true)" = "$ORIGIN" ] && [ -z "$(git -C "$SOURCE" status --porcelain)" ] || { echo "managed source is untrusted or dirty" >&2; exit 1; }
    if [ -n "$BREW_BACKUP" ]; then
      source_install || { rc=$?; FAILED="$ROOT/staging/failed-$$"; mv "$SOURCE" "$FAILED" && mv "$BREW_BACKUP" "$SOURCE" || { [ -d "$SOURCE" ] || [ ! -d "$BREW_BACKUP" ] || mv "$BREW_BACKUP" "$SOURCE"; echo "Homebrew release $BREW_TAG failed and could not be restored" >&2; exit "$rc"; }; if source_install 1; then note="prior payload restored"; else note="prior checkout restored but payload reconverge failed"; fi; rm -rf "$FAILED"; echo "Homebrew release $BREW_TAG failed; $note" >&2; exit "$rc"; }
      rm -rf "$BREW_BACKUP"; exit 0
    fi
    source_install; exit $?
  }
}
[ "$NO_BOOTSTRAP" = 1 ] || [ "$DRY" = 1 ] || [ "$(git -C "$SOURCE" remote get-url origin 2>/dev/null || true)" = "$ORIGIN" ] || { echo "managed source origin is not trusted" >&2; exit 1; }
TAILSCALE=$(command -v tailscale || true)
[ -n "$TAILSCALE" ] || [ ! -x /Applications/Tailscale.app/Contents/MacOS/Tailscale ] || TAILSCALE=/Applications/Tailscale.app/Contents/MacOS/Tailscale
[ "$SERVE" = 0 ] || [ -n "$TAILSCALE" ] || { echo "Tailscale CLI is required to publish Serve paths" >&2; exit 1; }
# Agents-mode preflight — all-or-nothing BEFORE any mutation, so a missing
# brew prerequisite is a clear error, never a half-install.
T3_BIN="" TTYD_BIN="" JAIL="$HOME/Grave"
AGENT_PATH="/opt/homebrew/bin:/usr/local/bin:$HOME_CANON/.local/bin:/usr/bin:/bin"
if [ "$WANT_AGENTS" = 1 ]; then
  MISSING=""
  TMUX_BIN=$(command -v tmux || true); [ -n "$TMUX_BIN" ] || MISSING="$MISSING tmux"
  TTYD_BIN=$(command -v ttyd || true); [ -n "$TTYD_BIN" ] || MISSING="$MISSING ttyd"
  NODE_BIN=$(command -v node || true); command -v npm >/dev/null || NODE_BIN=""
  [ -n "$NODE_BIN" ] || MISSING="$MISSING node"
  [ -z "$MISSING" ] || { echo "--agents needs brew prerequisites:$MISSING (brew install$MISSING)" >&2; exit 1; }
  # The updater's fixed PATH may not see a user-prefix npm t3 — check the
  # plists' durable location before deciding to (re)install it.
  T3_BIN=$(command -v t3 || true)
  [ -n "$T3_BIN" ] || [ ! -x "$HOME/.local/bin/t3" ] || T3_BIN="$HOME/.local/bin/t3"
  if [ -z "$T3_BIN" ] && [ "$DRY" = 0 ]; then
    npm install -g t3 || { echo "npm install -g t3 failed — fix npm's global prefix and rerun" >&2; exit 1; }
    T3_BIN=$(command -v t3 || true)
    [ -n "$T3_BIN" ] || { echo "t3 still not on PATH after npm install -g t3" >&2; exit 1; }
  fi
  # Bake the preflight-resolved tool dirs into the agents' launchd PATH:
  # MacPorts tmux or an nvm node (t3's #!/usr/bin/env node) that only the
  # operator's interactive shell can see must still resolve under launchd.
  AGENT_PATH="$(dirname "$TMUX_BIN"):$(dirname "$NODE_BIN")${T3_BIN:+:$(dirname "$T3_BIN")}:$AGENT_PATH"
fi
ALLOWED_USERS=""
if [ "$SERVE" = 1 ] && [ "$UPDATER" = 1 ] && [ -f "$ROOT/config/allowed-user" ]; then
  ALLOWED_USERS=$(cat "$ROOT/config/allowed-user")
elif [ "$SERVE" = 1 ]; then
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
[ "$SERVE" = 0 ] || case "$ALLOWED_USERS" in ""|*[!A-Za-z0-9._+@-]*) echo "Invalid preserved local Tailscale login; refusing to enable Serve" >&2; exit 1;; esac
# Agents mode publishes the '/' Serve mount. Refuse to seize one that belongs
# to something else (official T3 app, another tool) — the same "refuses
# collisions" invariant docs/PORTS.md records for the native Mac app.
if [ "$WANT_AGENTS" = 1 ] && [ "$SERVE" = 1 ]; then
  "$TAILSCALE" serve status --json 2>/dev/null | "$PYTHON" -c '
import json, sys
try:
    cfg = json.load(sys.stdin)
except Exception:
    raise SystemExit(0)   # no Serve config yet
for site in (cfg.get("Web") or {}).values():
    proxy = ((site.get("Handlers") or {}).get("/") or {}).get("Proxy")
    if proxy and proxy not in ("http://127.0.0.1:4711", "http://localhost:4711"):
        raise SystemExit(1)
' || { echo "the / Serve mount is already published by something else; refusing --agents (tailscale serve status)" >&2; exit 1; }
fi
UID_NOW=$(id -u); AGENTS="$HOME/Library/LaunchAgents"
run(){ if [ "$DRY" = 1 ]; then printf 'dry-run: '; printf '%s ' "$@"; printf '\n'; else "$@"; fi; }
APPS=""; [ "$WANT_NET" = 0 ] || APPS="📡 Network=/net/"
# Agents mode gets the appliance's launcher tiles (T3, terminal, agent CLIs)
# in front of the observability tiles, matching the Linux origin layout.
[ "$WANT_AGENTS" = 0 ] || APPS="⌨️ T3 Code=/;🖥️ Terminal=/term/?arg=shell;🤖 Claude=/term/?arg=claude;🧠 Codex=/term/?arg=codex${APPS:+;$APPS}"
render(){ template=$1; target=$2; if [ "$DRY" = 1 ]; then echo "dry-run: render $template -> $target"; else sed "s|@PYTHON@|$PYTHON|g;s|@ROOT@|$ROOT|g;s|@HOME@|$HOME_CANON|g;s|@APPS@|$APPS|g;s|@ALLOWED_USERS@|$ALLOWED_USERS|g;s|@AGENTS@|$WANT_AGENTS|g;s|@T3@|$T3_BIN|g;s|@TTYD@|$TTYD_BIN|g;s|@JAIL@|$JAIL|g;s|@AGENT_PATH@|$AGENT_PATH|g" "$template" > "$target"; plutil -lint "$target" >/dev/null; fi; }
load(){ render "$HERE/LaunchAgents/$1.plist.tmpl" "$AGENTS/$1.plist"; run launchctl bootout "gui/$UID_NOW" "$AGENTS/$1.plist" 2>/dev/null || true; run launchctl bootstrap "gui/$UID_NOW" "$AGENTS/$1.plist"; }
unload(){ label=$1; plist="$AGENTS/$label.plist"; [ -e "$plist" ] && run launchctl bootout "gui/$UID_NOW" "$plist" 2>/dev/null || true; run rm -f "$plist"; }
serve_off(){ path=$1; [ -z "$TAILSCALE" ] || run "$TAILSCALE" serve --https=443 --set-path="$path" off; }
[ "$DRY" = 1 ] || { run mkdir -p "$ROOT/scripts" "$ROOT/web/net" "$ROOT/logs" "$ROOT/config" "$ROOT/config/secrets" "$ROOT/repos" "$ROOT/staging" "$AGENTS"; run chmod 700 "$ROOT/config/secrets"; : > "$ROOT/.gravedecay-macos"; }
if [ "$DRY" = 0 ]; then
  run cp "$HERE/grave" "$ROOT/scripts/grave"; run cp "$HERE/updater.py" "$ROOT/scripts/updater.py"; run cp "$HERE/status.sh" "$ROOT/scripts/status.sh"; run chmod 700 "$ROOT/scripts/grave" "$ROOT/scripts/updater.py" "$ROOT/scripts/status.sh"
  "$PYTHON" - "$ROOT" "$SOURCE" "${GRAVEDECAY_UPDATE_CHANNEL:-release}" <<'PY'
import json, os, subprocess, sys
root, source, channel = sys.argv[1:]
def git(*args): return subprocess.run(["git", "-C", source, *args], text=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL).stdout.strip()
current = git("describe", "--tags", "--exact-match", "HEAD") if os.path.isdir(os.path.join(source, ".git")) and channel != "edge" else ""
checkout = current or (git("branch", "--show-current") or git("rev-parse", "--short", "HEAD"))
kind = "edge" if channel == "edge" else ("release" if current else "development")
with open(os.path.join(root, "config", "release.json.tmp"), "w") as f: json.dump({"current":current,"checkout":checkout,"channel":channel,"kind":kind,"origin":"https://github.com/projectmushroom/gravedecay.git"}, f)
os.replace(os.path.join(root, "config", "release.json.tmp"), os.path.join(root, "config", "release.json"))
PY
  if [ "$SERVE" = 1 ] && [ "$UPDATER" != 1 ]; then printf '%s\n' "$ALLOWED_USERS" > "$ROOT/config/allowed-user"; chmod 600 "$ROOT/config/allowed-user"; fi
  HOOK="$HOME/.local/bin/grave"; mkdir -p "$HOME/.local/bin"
  if [ ! -e "$HOOK" ] || [ "$(readlink "$HOOK" 2>/dev/null || true)" = "$ROOT/scripts/grave" ]; then ln -snf "$ROOT/scripts/grave" "$HOOK"; else echo "grave hook not installed: $HOOK is already owned by another command" >&2; fi
fi
if [ "$WANT_DASH" = 1 ]; then
  run cp "$HERE/../dashboard/gravedecay.py" "$ROOT/scripts/gravedecay.py"
  run mkdir -p "$ROOT/scripts/dashboard-static"
  run cp "$HERE/../dashboard/static/"* "$ROOT/scripts/dashboard-static/"
  run cp "$HERE/../assets/gravedecay.png" "$ROOT/config/gravedecay.png"
  load io.gravedecay.dashboard
else unload io.gravedecay.dashboard; serve_off /grave; fi
if [ "$WANT_NET" = 1 ]; then
  run cp "$HERE/../dashboard/gravenet.py" "$ROOT/scripts/gravenet.py"; run cp "$HERE/../web/net/index.html" "$ROOT/web/net/index.html"
  load io.gravedecay.network
else unload io.gravedecay.network; serve_off /net; fi
if [ "$WANT_AGENTS" = 1 ]; then
  run mkdir -p "$ROOT/agents/t3code" "$ROOT/web/term" "$JAIL"
  run cp "$HERE/../bin/webterm" "$ROOT/scripts/webterm"; run chmod 700 "$ROOT/scripts/webterm"
  run cp "$HERE/../config/tmux.conf" "$ROOT/config/tmux.conf"
  # Same clipboard-capable ttyd frontend as the appliance (pure splice, no network).
  if [ "$DRY" = 1 ]; then echo "dry-run: build term frontend -> $ROOT/web/term/index.html"
  else "$PYTHON" "$HERE/../docker/portable/build-term.py" "$HERE/../web/term" "$ROOT/web/term/index.html"; fi
  load io.gravedecay.t3; load io.gravedecay.term
elif [ "$OLD_AGENTS" = 1 ]; then
  # Converge-by-omission back to observability-only. Only tear down the Serve
  # mounts this installer created in agents mode — a '/' mount on a Mac that
  # never opted in is someone else's and must never be touched.
  unload io.gravedecay.t3; unload io.gravedecay.term
  serve_off /; serve_off /term
  # Never kill work implicitly — but do say when sessions are left running
  # with no remote surface to observe or end them.
  if [ "$DRY" = 0 ] && command -v tmux >/dev/null && tmux -L agents list-sessions >/dev/null 2>&1; then
    echo "note: tmux -L agents still has live sessions and no remote surface — 'tmux -L agents attach' or 'tmux -L agents kill-server'" >&2
  fi
fi
if [ "$UPDATER" != 1 ]; then
  load io.gravedecay.updater
fi
# Serving Macs must not idle-sleep (the tailnet paths just go dark) — hold a
# caffeinate assertion for as long as launchd runs. --allow-sleep opts out.
if [ "$SERVE" = 1 ] && [ "$KEEPAWAKE" = 1 ]; then
  load io.gravedecay.keepawake
else unload io.gravedecay.keepawake; fi
# Periodic doctor-lite; a failing contract pages via `grave notify --event
# doctor`, a silent no-op until an ntfy topic is configured.
load io.gravedecay.doctor
# Record the converged component set BEFORE the health gates: if a probe
# below aborts, the metadata still says what is actually loaded, so doctor
# enforces it, the updater restates it, and a rerun can converge it off —
# never a half-install that no gravedecay tool admits exists.
[ "$DRY" = 1 ] || printf 'dashboard=%s\nnetwork=%s\nserve=%s\nkeepawake=%s\nagents=%s\n' "$WANT_DASH" "$WANT_NET" "$SERVE" "$KEEPAWAKE" "$WANT_AGENTS" > "$ROOT/config/components"
# --max-time is not optional (see raise.sh wait_http): a curl that connects
# while the service is mid-start can hang on a never-arriving response.
health(){ port=$1; path=${2:-/healthz}; i=0; while [ "$i" -lt 20 ]; do curl -fs --max-time 5 "http://127.0.0.1:$port$path" >/dev/null 2>&1 && return 0; i=$((i+1)); sleep 1; done; echo "127.0.0.1:$port$path not answering after 20s" >&2; return 1; }
if [ "$DRY" = 0 ]; then [ "$WANT_DASH" = 0 ] || health 4712; [ "$WANT_NET" = 0 ] || health 4714; fi
# t3/ttyd have no /healthz; an answering root page is their liveness signal.
# Non-fatal, like the Linux wait_http: a cold npm-installed t3 can outlast
# the poll window, and doctor-lite enforces steady state either way.
if [ "$DRY" = 0 ] && [ "$WANT_AGENTS" = 1 ]; then
  health 4711 / || echo "warning: t3 not answering on :4711 yet — check $ROOT/logs/t3.error.log; macos/status.sh enforces it" >&2
  health 4713 / || echo "warning: ttyd not answering on :4713 — check $ROOT/logs/term.error.log; macos/status.sh enforces it" >&2
fi
if [ "$SERVE" = 1 ]; then
  [ "$WANT_DASH" = 1 ] && run "$TAILSCALE" serve --bg --https=443 --set-path=/grave http://127.0.0.1:4712
  [ "$WANT_NET" = 1 ] && run "$TAILSCALE" serve --bg --https=443 --set-path=/net http://127.0.0.1:4714
  if [ "$WANT_AGENTS" = 1 ]; then
    run "$TAILSCALE" serve --bg --https=443 --set-path=/ http://127.0.0.1:4711
    run "$TAILSCALE" serve --bg --https=443 --set-path=/term http://127.0.0.1:4713
  fi
else
  [ "$WANT_DASH" = 0 ] || serve_off /grave
  [ "$WANT_NET" = 0 ] || serve_off /net
  # Remove / and /term only when a previous agents+Serve install created
  # them; on a fresh --agents --no-serve there is nothing of ours to remove
  # and a foreign '/' mount must not be touched.
  if [ "$WANT_AGENTS" = 1 ] && [ "$OLD_AGENTS" = 1 ] && [ "${OLD_SERVE:-0}" = 1 ]; then serve_off /; serve_off /term; fi
fi
if [ "$WANT_AGENTS" = 1 ]; then
  echo "Installed macOS companion with the agents layer at $ROOT. Pair devices from ⚙️ settings; sessions live in tmux -L agents and start in $JAIL."
else
  echo "Installed macOS dashboard/network companion at $ROOT. Add $HOME/.local/bin to PATH to use grave. T3 is intentionally unmanaged."
fi
