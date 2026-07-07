#!/usr/bin/env bash
# raise.sh — the gravedecay ritual. Idempotent bootstrap: run as your normal
# user (sudo is used where needed), rerun freely after fixing any failure.
#
#   ./raise.sh [--profile <generic|t2-macbook|steam-machine|...>] [--root <dir>]
#
# Designed to be agent-supervised: it does the deterministic 90 %, prints
# clearly what it skipped, and leaves distro oddities to you/your agent.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
GRAVE_ROOT="/srv/dev"
PROFILE=""
RUN_USER="${SUDO_USER:-$USER}"
HOME_DIR=$(getent passwd "$RUN_USER" | cut -d: -f6)
T3_PORT=4711
DASH_PORT=4712
TERM_PORT=4713

while [[ $# -gt 0 ]]; do
  case "$1" in
    --profile) PROFILE="$2"; shift 2 ;;
    --root)    GRAVE_ROOT="$2"; shift 2 ;;
    -h|--help) sed -n '2,9p' "$0"; exit 0 ;;
    *) echo "unknown arg: $1"; exit 1 ;;
  esac
done

GRN=$'\e[32m'; YLW=$'\e[33m'; BLD=$'\e[1m'; RST=$'\e[0m'
step() { printf '\n%b🪦 %s%b\n' "$BLD" "$*" "$RST"; }
ok()   { printf '  %b✓%b %s\n' "$GRN" "$RST" "$*"; }
skip() { printf '  %b–%b %s\n' "$YLW" "$RST" "$*"; }

[[ $EUID -eq 0 ]] && { echo "Run as your normal user, not root (sudo is used internally)."; exit 1; }
sudo -n true 2>/dev/null || sudo -v || { echo "sudo access required"; exit 1; }

# ----------------------------------------------- 0. environment detection ----
# Immutable rootfs (stock SteamOS, Silverblue, …): /usr is read-only and an OS
# update REPLACES it, so we must never install into it — the toolchain lives
# under $HOME via Homebrew + rootless Docker, and GRAVE_ROOT moves off /srv
# (which rides the root image) onto persistent $HOME. See docs/STEAMOS.md.
IMMUTABLE=0
if command -v steamos-readonly >/dev/null 2>&1 && steamos-readonly status 2>/dev/null | grep -qx enabled; then
  IMMUTABLE=1
elif findmnt -no OPTIONS / 2>/dev/null | grep -qw ro; then
  IMMUTABLE=1
fi

BREW_PREFIX=""
for p in /home/linuxbrew/.linuxbrew "$HOME_DIR/.linuxbrew"; do
  [[ -x "$p/bin/brew" ]] && { BREW_PREFIX="$p"; break; }
done
[[ -n "$BREW_PREFIX" ]] && eval "$("$BREW_PREFIX/bin/brew" shellenv bash)" 2>/dev/null || true

# A "managed toolchain" host keeps its dev tools under $HOME (Homebrew), not in
# the system package manager. Immutable rootfs implies it; a present Homebrew
# opts in explicitly.
MANAGED_TOOLCHAIN=0
[[ "$IMMUTABLE" == 1 || -n "$BREW_PREFIX" ]] && MANAGED_TOOLCHAIN=1

export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"

# Relocate GRAVE_ROOT off an immutable root image onto persistent $HOME.
if [[ "$IMMUTABLE" == 1 && "$GRAVE_ROOT" == "/srv/dev" ]]; then
  GRAVE_ROOT="$HOME_DIR/gravedecay"
fi

# Tool binaries + an extra PATH so systemd services (and raise's own lookups)
# find the durable toolchain that lives under $HOME.
TOOLPATH=""
if [[ "$MANAGED_TOOLCHAIN" == 1 && -n "$BREW_PREFIX" ]]; then
  # native addons (t3's node-pty) are built against a pinned Node LTS keg —
  # prefer it so `env node` in the services resolves to the matching ABI
  T3_NODE_DIR="$(ls -d "$BREW_PREFIX"/opt/node@*/bin 2>/dev/null | sort -V | tail -1)"
  [[ -z "${T3_NODE_DIR:-}" ]] && T3_NODE_DIR="$(dirname "$(command -v node 2>/dev/null || echo /usr/bin/node)")"
  TOOLPATH="$T3_NODE_DIR:$BREW_PREFIX/bin:$HOME_DIR/bin:"
fi
export PATH="$HOME_DIR/.local/bin:$TOOLPATH$PATH"

TTYD_BIN="$(command -v ttyd 2>/dev/null || echo /usr/bin/ttyd)"
T3_BIN="$(command -v t3 2>/dev/null || echo /usr/bin/t3)"

# The grave CLI normally installs to /usr/local/bin, but on an immutable rootfs
# that is read-only — install into the user's ~/.local/bin (durable, on PATH)
# instead, and thread the path through the dashboard + sudoers.
if [[ "$IMMUTABLE" == 1 ]]; then
  GRAVE_BIN="$HOME_DIR/.local/bin/grave"
else
  GRAVE_BIN="/usr/local/bin/grave"
fi

# Rootless Docker? (per-user daemon, no sudo — the durable choice on SteamOS)
DOCKER_ROOTLESS=0
DOCKER_HOSTV=""
if docker context inspect rootless >/dev/null 2>&1 || [[ -S "/run/user/$(id -u)/docker.sock" ]]; then
  DOCKER_ROOTLESS=1
  DOCKER_HOSTV="unix:///run/user/$(id -u)/docker.sock"
  export DOCKER_HOST="$DOCKER_HOSTV"
fi

# System units the dashboard reports. Rootless docker is a --user unit, so it's
# dropped from the system list (the Docker panel still shows it via `docker ps`).
if [[ "$DOCKER_ROOTLESS" == 1 ]]; then
  UNITS="t3code,gravedecay,gravedecay-term,tailscaled,sshd"
else
  UNITS="t3code,gravedecay,gravedecay-term,tailscaled,sshd,docker"
fi

# ------------------------------------------------------------ 1. packages ----
step "Packages"
if [[ "$MANAGED_TOOLCHAIN" == 1 ]]; then
  # Immutable / Homebrew host: the toolchain lives under $HOME and must NOT be
  # installed into read-only /usr (an OS update would erase it). Verify the
  # durable tools are present — bootstrap them first per docs/STEAMOS.md.
  missing=()
  for t in git tmux curl jq python3 node npm docker ttyd t3; do
    command -v "$t" >/dev/null 2>&1 || missing+=("$t")
  done
  if ((${#missing[@]})); then
    skip "durable toolchain incomplete — missing: ${missing[*]}"
    echo "  Bootstrap it under \$HOME first (Homebrew + rootless Docker):"
    echo "  see docs/STEAMOS.md, then rerun ./raise.sh --profile steam-machine"
    exit 1
  fi
  ok "durable toolchain present (Homebrew + rootless Docker, under \$HOME)"
elif command -v pacman >/dev/null; then
  sudo pacman -S --needed --noconfirm git tmux curl jq python docker docker-compose \
    nodejs npm ufw lm_sensors python-pillow ttyd
  ok "packages present"
elif command -v apt-get >/dev/null; then
  sudo apt-get update -qq
  sudo apt-get install -y git tmux curl jq python3 docker.io docker-compose-v2 \
    nodejs npm ufw lm-sensors python3-pil ttyd || skip "some packages failed — fix names for your distro and rerun"
  ok "packages present"
elif command -v dnf >/dev/null; then
  sudo dnf install -y git tmux curl jq python3 docker docker-compose nodejs npm \
    lm_sensors python3-pillow || skip "some packages failed — fix names for your distro and rerun"
  command -v ttyd >/dev/null || skip "ttyd not in Fedora repos — build/install it manually for the web terminal"
  ok "packages present"
else
  skip "unknown package manager — install git tmux curl jq python3 docker nodejs npm manually"
fi

# -------------------------------------------------------------- 2. layout ----
step "Layout at $GRAVE_ROOT"
sudo mkdir -p "$GRAVE_ROOT"/{repos,agents,docker,config/secrets,logs,scripts,backups,docs}
sudo chown -R "$RUN_USER:$RUN_USER" "$GRAVE_ROOT"
chmod 700 "$GRAVE_ROOT/config/secrets"
if [[ ! -e "$HOME_DIR/Projects" ]]; then
  ln -s "$GRAVE_ROOT/repos" "$HOME_DIR/Projects"
  ok "~/Projects → $GRAVE_ROOT/repos"
else
  skip "~/Projects already exists"
fi
cp -n "$REPO_DIR/config/tmux.conf" "$GRAVE_ROOT/config/tmux.conf" 2>/dev/null || true
cp "$REPO_DIR/docs/"*.md "$GRAVE_ROOT/docs/" 2>/dev/null || true
ok "layout ready"

# ------------------------------------------------------- 3. grave CLI+conf ----
step "grave CLI"
if [[ "$IMMUTABLE" == 1 ]]; then
  mkdir -p "$(dirname "$GRAVE_BIN")"
  install -m 755 "$REPO_DIR/bin/grave" "$GRAVE_BIN"   # /usr/local is read-only here
else
  sudo install -m 755 "$REPO_DIR/bin/grave" "$GRAVE_BIN"
fi
sudo mkdir -p /etc/gravedecay
if [[ ! -f /etc/gravedecay/grave.conf ]]; then
  sed -e "s|@GRAVE_ROOT@|$GRAVE_ROOT|g" \
      -e "s|@DOCKER_ROOTLESS@|$DOCKER_ROOTLESS|g" \
      -e "s|@DOCKER_HOST@|$DOCKER_HOSTV|g" \
      -e "s|@TOOL_PATH@|$TOOLPATH|g" \
      "$REPO_DIR/config/grave.conf.example" \
    | sudo tee /etc/gravedecay/grave.conf >/dev/null
  ok "installed /etc/gravedecay/grave.conf"
else
  skip "/etc/gravedecay/grave.conf exists — not overwritten"
fi

# ------------------------------------------------------------- 4. sudoers ----
step "Scoped passwordless sudo (see docs/SECURITY.md)"
sudo tee /etc/sudoers.d/50-gravedecay >/dev/null <<EOF
# gravedecay: let $RUN_USER (and gravedecay action buttons) drive the platform
$RUN_USER ALL=(root) NOPASSWD: /usr/bin/systemctl, /usr/bin/docker, $GRAVE_BIN, /usr/bin/journalctl, /usr/bin/ufw, /usr/bin/snapper, /usr/sbin/sshd -T, /usr/bin/sshd -T, /usr/bin/tee /etc/systemd/system/*, /usr/bin/tee /sys/fs/cgroup/grave-torpor/*, /usr/bin/mkdir -p /sys/fs/cgroup/grave-torpor, /usr/bin/npm update -g *
EOF
sudo chmod 440 /etc/sudoers.d/50-gravedecay
sudo visudo -c -f /etc/sudoers.d/50-gravedecay >/dev/null && ok "sudoers valid"

# ----------------------------------------------------------- 5. gravedecay ----
step "gravedecay"
install -m 755 "$REPO_DIR/dashboard/gravedecay.py" "$GRAVE_ROOT/scripts/gravedecay.py"
install -m 644 "$REPO_DIR/assets/gravedecay.png" "$GRAVE_ROOT/config/gravedecay.png"
sed -e "s|@USER@|$RUN_USER|g" -e "s|@GRAVE_ROOT@|$GRAVE_ROOT|g" \
    -e "s|@DASH_PORT@|$DASH_PORT|g" -e "s|@ALLOWED_USERS@||g" \
    -e "s|@TOOLPATH@|$TOOLPATH|g" -e "s|@DOCKER_HOST@|$DOCKER_HOSTV|g" \
    -e "s|@UNITS@|$UNITS|g" -e "s|@GRAVE_BIN@|$GRAVE_BIN|g" \
    "$REPO_DIR/systemd/gravedecay.service.tmpl" | sudo tee /etc/systemd/system/gravedecay.service >/dev/null
# drop an empty DOCKER_HOST= line on system-docker hosts (empty would confuse the CLI)
sudo sed -i '/^Environment=DOCKER_HOST=$/d' /etc/systemd/system/gravedecay.service
sudo systemctl daemon-reload
sudo systemctl enable --now gravedecay
curl -sf -o /dev/null "http://127.0.0.1:$DASH_PORT/healthz" && ok "gravedecay answering on :$DASH_PORT"

# --------------------------------------------------------- 5b. web terminal ----
step "Web terminal (ttyd → tmux agents socket)"
if command -v ttyd >/dev/null; then
  install -m 755 "$REPO_DIR/bin/webterm" "$GRAVE_ROOT/scripts/webterm"
  sed -e "s|@USER@|$RUN_USER|g" -e "s|@GRAVE_ROOT@|$GRAVE_ROOT|g" \
      -e "s|@TERM_PORT@|$TERM_PORT|g" -e "s|@HOME@|$HOME_DIR|g" \
      -e "s|@TTYD@|$TTYD_BIN|g" -e "s|@TOOLPATH@|$TOOLPATH|g" \
      -e "s|@DOCKER_HOST@|$DOCKER_HOSTV|g" \
      "$REPO_DIR/systemd/gravedecay-term.service.tmpl" | sudo tee /etc/systemd/system/gravedecay-term.service >/dev/null
  sudo sed -i '/^Environment=DOCKER_HOST=$/d' /etc/systemd/system/gravedecay-term.service
  sudo systemctl daemon-reload
  sudo systemctl enable --now gravedecay-term
  curl -sf -o /dev/null "http://127.0.0.1:$TERM_PORT/" && ok "web terminal answering on :$TERM_PORT"
else
  skip "ttyd missing — web terminal not installed"
fi

# ------------------------------------------------------------- 6. T3 Code ----
step "T3 Code"
# On a managed-toolchain host t3 is already installed under $HOME (with a
# native node-pty built against the pinned Node LTS — see docs/STEAMOS.md); the
# plain `npm install -g t3` fallback only applies to package-manager hosts.
if ! command -v t3 >/dev/null; then
  if [[ "$MANAGED_TOOLCHAIN" == 1 ]]; then
    skip "t3 not found under \$HOME — install it per docs/STEAMOS.md and rerun"; exit 1
  fi
  sudo npm install -g t3
fi
sed -e "s|@USER@|$RUN_USER|g" -e "s|@GRAVE_ROOT@|$GRAVE_ROOT|g" \
    -e "s|@T3_PORT@|$T3_PORT|g" -e "s|@HOME@|$HOME_DIR|g" \
    -e "s|@T3@|$T3_BIN|g" -e "s|@TOOLPATH@|$TOOLPATH|g" \
    "$REPO_DIR/systemd/t3code.service.tmpl" | sudo tee /etc/systemd/system/t3code.service >/dev/null
sudo systemctl daemon-reload
sudo systemctl enable --now t3code
sleep 2
curl -sf -o /dev/null "http://127.0.0.1:$T3_PORT/" && ok "t3code answering on :$T3_PORT" \
  || skip "t3code not answering yet — check: grave logs t3"

# -------------------------------------------------------------- 7. docker ----
step "Docker stacks"
if [[ "$DOCKER_ROOTLESS" == 1 ]]; then
  # rootless: per-user daemon (no sudo), enabled via the user manager + linger
  systemctl --user enable --now docker >/dev/null 2>&1 || true
  DC=(docker)
else
  sudo systemctl enable --now docker
  DC=(sudo docker)
fi
"${DC[@]}" network inspect devnet >/dev/null 2>&1 || "${DC[@]}" network create devnet >/dev/null
for stack in core browsers; do
  if [[ ! -d "$GRAVE_ROOT/docker/$stack" ]]; then
    cp -r "$REPO_DIR/docker/$stack" "$GRAVE_ROOT/docker/$stack"
    ok "stack $stack installed"
  else
    skip "stack $stack exists — not overwritten"
  fi
done
if [[ ! -f "$GRAVE_ROOT/docker/core/.env" ]]; then
  printf 'POSTGRES_PASSWORD=%s\n' "$(tr -dc 'A-Za-z0-9' </dev/urandom | head -c 32)" \
    > "$GRAVE_ROOT/docker/core/.env"
  chmod 600 "$GRAVE_ROOT/docker/core/.env"
  ok "generated core/.env with random postgres password"
fi
"${DC[@]}" compose --project-directory "$GRAVE_ROOT/docker/core" up -d --wait && ok "core stack up"
"${DC[@]}" compose --project-directory "$GRAVE_ROOT/docker/browsers" up -d && ok "browsers stack up"

# ------------------------------------------------------------ 8. firewall ----
step "Firewall (default-deny, SSH allowed BEFORE enabling)"
if command -v ufw >/dev/null; then
  sudo ufw allow ssh >/dev/null
  sudo ufw allow in on tailscale0 >/dev/null 2>&1 || true
  sudo ufw default deny incoming >/dev/null
  sudo ufw default allow outgoing >/dev/null
  sudo ufw --force enable >/dev/null
  ok "ufw active (deny in, allow ssh + tailscale0)"
elif command -v firewall-cmd >/dev/null; then
  # A gaming box (steam-machine profile) needs LAN reachable for Steam Remote
  # Play / local multiplayer / discovery, so we do NOT impose a host-wide
  # default-deny here. The security boundary is: every gravedecay service binds
  # 127.0.0.1 and is reachable only via `tailscale serve`; sshd is key-only.
  # The steam-machine profile sets CHECK_FIREWALL=0 so doctor reflects this.
  # If you don't use LAN gaming, harden with firewalld: default zone drop,
  # allow ssh + trust tailscale0.
  skip "firewalld present — leaving LAN open for Steam; boundary is 127.0.0.1 + tailnet (see profiles/steam-machine.sh)"
else
  skip "no firewall tool found — services still bind 127.0.0.1 + tailnet only"
fi

# ----------------------------------------------------------- 9. tailscale ----
step "Tailscale"
# On a managed-toolchain host tailscaled is the Homebrew binary with no unit —
# install a system service for it. State in /var/lib/tailscale survives OS
# updates, so the box stays on the tailnet across SteamOS updates.
if [[ "$MANAGED_TOOLCHAIN" == 1 ]] && command -v tailscaled >/dev/null && ! systemctl cat tailscaled.service >/dev/null 2>&1; then
  TSD="$(command -v tailscaled)"; TSCLI="$(command -v tailscale)"
  sudo tee /etc/systemd/system/tailscaled.service >/dev/null <<EOF
[Unit]
Description=Tailscale node agent
Documentation=https://tailscale.com/kb/
After=network-pre.target
Wants=network-pre.target
[Service]
ExecStart=$TSD --state=/var/lib/tailscale/tailscaled.state --socket=/run/tailscale/tailscaled.sock --port=41641
ExecStopPost=$TSCLI --socket=/run/tailscale/tailscaled.sock down
RuntimeDirectory=tailscale
StateDirectory=tailscale
Restart=on-failure
[Install]
WantedBy=multi-user.target
EOF
  sudo systemctl daemon-reload
  sudo systemctl enable --now tailscaled
  ok "installed tailscaled system unit (Homebrew binary, state in /var/lib/tailscale)"
fi
if ! command -v tailscale >/dev/null; then
  skip "tailscale not installed — install it, run 'sudo tailscale up --ssh', rerun raise.sh"
elif ! tailscale status --peers=false >/dev/null 2>&1; then
  skip "tailscale not logged in — run 'sudo tailscale up --ssh', rerun raise.sh"
else
  sudo tailscale set --operator="$RUN_USER" 2>/dev/null || true
  # one origin: T3 at the root, gravedecay mounted at /grave — gravedecay is the
  # entry point (install the PWA from /grave/), apps hop stays same-origin
  tailscale serve --bg --https=443 "http://127.0.0.1:$T3_PORT" >/dev/null && ok "T3 → https / on tailnet"
  tailscale serve --bg --https=443 --set-path=/grave "http://127.0.0.1:$DASH_PORT" >/dev/null && ok "gravedecay → https /grave on tailnet"
  command -v ttyd >/dev/null && tailscale serve --bg --https=443 --set-path=/term "http://127.0.0.1:$TERM_PORT" >/dev/null && ok "web terminal → https /term on tailnet"
fi

# ------------------------------------------------------------ 10. profile ----
if [[ -n "$PROFILE" ]]; then
  step "Host profile: $PROFILE"
  # shellcheck disable=SC1090
  source "$REPO_DIR/profiles/$PROFILE.sh"
  profile_apply
  ok "profile applied"
fi

# ------------------------------------------------------------- 11. doctor ----
step "Doctor"
grave doctor || skip "doctor has failures — fix and rerun 'grave doctor'"

printf '\n%b🪦 The box is raised.%b Next: pair a device (t3 auth pairing), add secrets (docs/SECRETS.md).\n' "$BLD" "$RST"
