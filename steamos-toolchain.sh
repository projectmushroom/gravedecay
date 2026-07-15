#!/usr/bin/env bash
# steamos-toolchain.sh — bootstrap gravedecay's durable toolchain on stock
# SteamOS (Steam Machine / Steam Deck), then hand off to raise.sh.
#
#   ./steamos-toolchain.sh          # install the toolchain under $HOME
#   ./raise.sh --profile steam-machine
#
# WHY THIS EXISTS. SteamOS has an immutable, image-based rootfs: `/usr` is
# read-only and an OS update REPLACES it, so anything pacman'd into the system
# is wiped on every update. Everything here therefore lives under $HOME (via
# Homebrew) or /var (docker/tailscale state) — the partitions an update leaves
# alone — so the appliance survives SteamOS updates untouched. Idempotent:
# rerun any time.
#
# Requires: passwordless (or cached) sudo, and network access.
set -euo pipefail

GRN=$'\e[32m'; YLW=$'\e[33m'; BLD=$'\e[1m'; RST=$'\e[0m'
step() { printf '\n%b🪦 %s%b\n' "$BLD" "$*" "$RST"; }
ok()   { printf '  %b✓%b %s\n' "$GRN" "$RST" "$*"; }
say()  { printf '  %b–%b %s\n' "$YLW" "$RST" "$*"; }

# Third-party bootstrap installers, pinned to an exact commit and verified by
# sha256 — never `curl | sh` of a moving HEAD, which (combined with this box's
# passwordless sudo) makes a compromised upstream or MITM'd CDN root-equivalent.
# To bump: change the commit, then update the digest to the new script's sha256.
BREW_INSTALLER_COMMIT=4b0227cf8416504142d23893368c2e1d211d5191
BREW_INSTALLER_SHA256=99287f194a8b3c9e6b0203a11a5fa54518be57209343e6bb954dec4635796d9d
DOCKER_ROOTLESS_COMMIT=02cb80d6c7d24c85a458ae31d166a6c535c7a37a
DOCKER_ROOTLESS_SHA256=519165a123f9924c530c64bdba3019124555eb311a671e149e2d1c1f79a6a92d
fetch_verified() { # fetch_verified <url> <sha256> — prints a temp file holding the verified script
  local url="$1" want="$2" tmp; tmp=$(mktemp)
  curl -fsSL "$url" -o "$tmp" || { echo "download failed: $url" >&2; rm -f "$tmp"; exit 1; }
  local got; got=$(sha256sum "$tmp" | awk '{print $1}')
  [[ "$got" == "$want" ]] || { echo "checksum mismatch for $url (got $got want $want) — refusing to run" >&2; rm -f "$tmp"; exit 1; }
  printf '%s' "$tmp"
}

[[ $EUID -eq 0 ]] && { echo "Run as your normal user (deck), not root."; exit 1; }
sudo -n true 2>/dev/null || sudo -v || { echo "sudo access required"; exit 1; }

BREW_PREFIX=/home/linuxbrew/.linuxbrew
NODE_LTS=node@22          # native addons (t3's node-pty) are built against this
UID_NUM="$(id -u)"

# ------------------------------------------------------------ 1. Homebrew ----
step "Homebrew (durable toolchain root, under \$HOME)"
if [[ ! -x "$BREW_PREFIX/bin/brew" ]]; then
  brew_installer=$(fetch_verified \
    "https://raw.githubusercontent.com/Homebrew/install/$BREW_INSTALLER_COMMIT/install.sh" \
    "$BREW_INSTALLER_SHA256")
  NONINTERACTIVE=1 CI=1 /bin/bash "$brew_installer"; rm -f "$brew_installer"
  ok "Homebrew installed"
else
  say "Homebrew already present"
fi
eval "$("$BREW_PREFIX/bin/brew" shellenv bash)"
grep -q 'brew shellenv' "$HOME/.bashrc" 2>/dev/null || \
  printf '\neval "$(%s/bin/brew shellenv bash)"\n' "$BREW_PREFIX" >> "$HOME/.bashrc"

# ---------------------------------------------------------------- 2. tools ----
# node@22 (keg-only, pinned ABI for node-pty), ttyd (web terminal), jq, the
# tailscale binaries, slirp4netns (rootless-docker networking), and a compiler
# toolchain (gcc/make) + glibc & kernel headers — SteamOS ships NO C headers,
# so native npm modules can't compile without these.
step "Homebrew packages"
brew install "$NODE_LTS" jq ttyd tailscale slirp4netns docker-compose make gcc glibc linux-headers
ok "packages installed"

# --------------------------------------------------------- 3. rootless docker ----
step "Rootless Docker (per-user daemon under \$HOME)"
sudo loginctl enable-linger "$USER" >/dev/null 2>&1 || true   # user services persist w/o login
export XDG_RUNTIME_DIR="/run/user/$UID_NUM"
if [[ ! -x "$HOME/bin/dockerd-rootless.sh" ]]; then
  docker_installer=$(fetch_verified \
    "https://raw.githubusercontent.com/docker/docker-install/$DOCKER_ROOTLESS_COMMIT/rootless-install.sh" \
    "$DOCKER_ROOTLESS_SHA256")
  sh "$docker_installer"; rm -f "$docker_installer"
  ok "rootless docker installed"
else
  say "rootless docker already present"
fi
export PATH="$HOME/bin:$PATH"
export DOCKER_HOST="unix:///run/user/$UID_NUM/docker.sock"
# This kernel/fs rejects unprivileged native overlayfs, so use fuse-overlayfs
# (the standard rootless storage driver) and the classic graphdriver.
mkdir -p "$HOME/.config/docker"
if [[ ! -f "$HOME/.config/docker/daemon.json" ]]; then
  cat > "$HOME/.config/docker/daemon.json" <<'JSON'
{
  "features": { "containerd-snapshotter": false },
  "storage-driver": "fuse-overlayfs"
}
JSON
fi
systemctl --user enable --now docker >/dev/null 2>&1 || true
# the rootless bundle has no compose plugin — link the Homebrew one for `docker compose`
mkdir -p "$HOME/.docker/cli-plugins"
ln -sf "$(brew --prefix)/bin/docker-compose" "$HOME/.docker/cli-plugins/docker-compose"
docker info >/dev/null 2>&1 && ok "rootless docker answering" || say "docker not up yet — check: systemctl --user status docker"
docker compose version >/dev/null 2>&1 && ok "docker compose plugin linked" || say "docker compose not found"

# ----------------------------------------------------- 4. compiler wrappers ----
# SteamOS has no /usr/include, so gcc's own headers can't find the C library's
# via `#include_next`. Wrap gcc/g++ to append Homebrew's glibc + kernel headers
# with -idirafter (searched AFTER the builtin dirs, exactly where include_next
# looks). Compiling against Homebrew glibc (older than the system's) is
# runtime-safe: a shared .node resolves libc symbols against the process glibc.
step "Compiler wrappers (headerless-rootfs fix)"
GLIBC_INC="$(brew --prefix glibc)/include"
LINUXH_INC="$(brew --prefix linux-headers)/include"
GCC_BIN="$(ls "$BREW_PREFIX"/bin/gcc-[0-9]* 2>/dev/null | sort -V | tail -1)"
GXX_BIN="$(ls "$BREW_PREFIX"/bin/g++-[0-9]* 2>/dev/null | sort -V | tail -1)"
W="$HOME/.local/toolchain-wrappers"; mkdir -p "$W"
cat > "$W/cc"  <<EOF
#!/bin/sh
exec $GCC_BIN -idirafter $GLIBC_INC -idirafter $LINUXH_INC "\$@"
EOF
cat > "$W/c++" <<EOF
#!/bin/sh
exec $GXX_BIN -idirafter $GLIBC_INC -idirafter $LINUXH_INC "\$@"
EOF
chmod +x "$W/cc" "$W/c++"; ln -sf "$W/cc" "$W/gcc"; ln -sf "$W/c++" "$W/g++"
ok "wrappers at $W ($GCC_BIN)"

# -------------------------------------------------------------- 5. T3 Code ----
# Install under $HOME (~/.local), compiling node-pty against the pinned Node LTS
# with the wrapper compilers. The systemd unit raise.sh writes puts node@22 on
# PATH so `env node` matches the ABI this was built for.
step "T3 Code (native node-pty compiled for $NODE_LTS)"
# the install goes to ~/.local/bin, so that MUST be on the presence-check PATH —
# omitting it made `command -v t3` always miss and reinstall (unpinned) every run.
if ! PATH="$HOME/.local/bin:$W:$BREW_PREFIX/opt/$NODE_LTS/bin:$PATH" command -v t3 >/dev/null 2>&1 \
   || [[ ! -f "$HOME/.local/lib/node_modules/t3/dist/bin.mjs" ]]; then
  PATH="$W:$BREW_PREFIX/opt/$NODE_LTS/bin:$PATH" CC=cc CXX=c++ \
    "$BREW_PREFIX/opt/$NODE_LTS/bin/npm" install -g --prefix "$HOME/.local" t3
fi
PATH="$BREW_PREFIX/opt/$NODE_LTS/bin:$HOME/.local/bin:$PATH" t3 --version >/dev/null 2>&1 \
  && ok "t3 installed and runs" || { echo "t3 failed to run — see log above"; exit 1; }

printf '\n%b🪦 Toolchain ready.%b Next: ./raise.sh --profile steam-machine\n' "$BLD" "$RST"
