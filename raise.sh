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

# ------------------------------------------------------------ 1. packages ----
step "Packages"
if command -v pacman >/dev/null; then
  sudo pacman -S --needed --noconfirm git tmux curl jq python docker docker-compose \
    nodejs npm ufw lm_sensors python-pillow
elif command -v apt-get >/dev/null; then
  sudo apt-get update -qq
  sudo apt-get install -y git tmux curl jq python3 docker.io docker-compose-v2 \
    nodejs npm ufw lm-sensors python3-pil || skip "some packages failed — fix names for your distro and rerun"
elif command -v dnf >/dev/null; then
  sudo dnf install -y git tmux curl jq python3 docker docker-compose nodejs npm \
    lm_sensors python3-pillow || skip "some packages failed — fix names for your distro and rerun"
else
  skip "unknown package manager — install git tmux curl jq python3 docker nodejs npm manually"
fi
ok "packages present"

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
sudo install -m 755 "$REPO_DIR/bin/grave" /usr/local/bin/grave
sudo mkdir -p /etc/gravedecay
if [[ ! -f /etc/gravedecay/grave.conf ]]; then
  sed "s|@GRAVE_ROOT@|$GRAVE_ROOT|g" "$REPO_DIR/config/grave.conf.example" \
    | sudo tee /etc/gravedecay/grave.conf >/dev/null
  ok "installed /etc/gravedecay/grave.conf"
else
  skip "/etc/gravedecay/grave.conf exists — not overwritten"
fi

# ------------------------------------------------------------- 4. sudoers ----
step "Scoped passwordless sudo (see docs/SECURITY.md)"
sudo tee /etc/sudoers.d/50-gravedecay >/dev/null <<EOF
# gravedecay: let $RUN_USER (and gravedash action buttons) drive the platform
$RUN_USER ALL=(root) NOPASSWD: /usr/bin/systemctl, /usr/bin/docker, /usr/local/bin/grave, /usr/bin/journalctl, /usr/bin/ufw, /usr/bin/snapper, /usr/sbin/sshd -T, /usr/bin/sshd -T, /usr/bin/tee /etc/systemd/system/*, /usr/bin/npm update -g *
EOF
sudo chmod 440 /etc/sudoers.d/50-gravedecay
sudo visudo -c -f /etc/sudoers.d/50-gravedecay >/dev/null && ok "sudoers valid"

# ----------------------------------------------------------- 5. gravedash ----
step "gravedash"
install -m 755 "$REPO_DIR/dashboard/gravedash.py" "$GRAVE_ROOT/scripts/gravedash.py"
sed -e "s|@USER@|$RUN_USER|g" -e "s|@GRAVE_ROOT@|$GRAVE_ROOT|g" \
    -e "s|@DASH_PORT@|$DASH_PORT|g" -e "s|@ALLOWED_USERS@||g" \
    "$REPO_DIR/systemd/gravedash.service.tmpl" | sudo tee /etc/systemd/system/gravedash.service >/dev/null
sudo systemctl daemon-reload
sudo systemctl enable --now gravedash
curl -sf -o /dev/null "http://127.0.0.1:$DASH_PORT/healthz" && ok "gravedash answering on :$DASH_PORT"

# ------------------------------------------------------------- 6. T3 Code ----
step "T3 Code"
command -v t3 >/dev/null || sudo npm install -g t3
sed -e "s|@USER@|$RUN_USER|g" -e "s|@GRAVE_ROOT@|$GRAVE_ROOT|g" \
    -e "s|@T3_PORT@|$T3_PORT|g" -e "s|@HOME@|$HOME_DIR|g" \
    "$REPO_DIR/systemd/t3code.service.tmpl" | sudo tee /etc/systemd/system/t3code.service >/dev/null
sudo systemctl daemon-reload
sudo systemctl enable --now t3code
sleep 2
curl -sf -o /dev/null "http://127.0.0.1:$T3_PORT/" && ok "t3code answering on :$T3_PORT" \
  || skip "t3code not answering yet — check: grave logs t3"

# -------------------------------------------------------------- 7. docker ----
step "Docker stacks"
sudo systemctl enable --now docker
sudo docker network inspect devnet >/dev/null 2>&1 || sudo docker network create devnet >/dev/null
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
sudo docker compose --project-directory "$GRAVE_ROOT/docker/core" up -d --wait && ok "core stack up"
sudo docker compose --project-directory "$GRAVE_ROOT/docker/browsers" up -d && ok "browsers stack up"

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
  skip "firewalld detected — configure: default zone drop, allow ssh + tailscale0 (agent: do this now)"
else
  skip "no firewall tool found — install ufw and rerun"
fi

# ----------------------------------------------------------- 9. tailscale ----
step "Tailscale"
if ! command -v tailscale >/dev/null; then
  skip "tailscale not installed — install it, run 'sudo tailscale up --ssh', rerun raise.sh"
elif ! tailscale status --peers=false >/dev/null 2>&1; then
  skip "tailscale not logged in — run 'sudo tailscale up --ssh', rerun raise.sh"
else
  sudo tailscale set --operator="$RUN_USER" 2>/dev/null || true
  tailscale serve --bg --https=443  "http://127.0.0.1:$T3_PORT"  >/dev/null && ok "T3 → https 443 on tailnet"
  tailscale serve --bg --https=8443 "http://127.0.0.1:$DASH_PORT" >/dev/null && ok "gravedash → https 8443 on tailnet"
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
