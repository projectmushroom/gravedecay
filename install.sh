#!/usr/bin/env bash
# gravedecay bootstrap — the one-liner install:
#
#   curl -fsSL https://raw.githubusercontent.com/projectmushroom/gravedecay/master/install.sh | bash
#
# Extra args are passed to raise.sh:
#   ... | bash -s -- --profile t2-macbook
#
# Installs the latest RELEASE tag by default; GRAVEDECAY_CHANNEL=edge follows
# the main branch instead. After install, update with:  grave upgrade
set -euo pipefail

REPO_URL="${GRAVEDECAY_REPO:-https://github.com/projectmushroom/gravedecay}"
GRAVE_ROOT="${GRAVE_ROOT:-/srv/dev}"

[[ $EUID -eq 0 ]] && { echo "Run as your normal user (sudo is used internally)."; exit 1; }
if ! command -v git >/dev/null; then
  echo "🪦 git not found — installing it"
  if command -v dnf >/dev/null; then
    sudo dnf install -y git
  elif command -v apt-get >/dev/null; then
    sudo apt-get update -qq && sudo apt-get install -y git
  elif command -v pacman >/dev/null; then
    sudo pacman -S --needed --noconfirm git
  fi
  command -v git >/dev/null || { echo "Install git first, then re-run."; exit 1; }
fi

# Immutable rootfs (stock SteamOS, Silverblue): /srv rides the read-only root
# image — `mkdir /srv/dev` fails, and anything there is erased by the next OS
# update. Install under $HOME instead, matching where raise.sh relocates
# GRAVE_ROOT so its own detection agrees. See docs/STEAMOS.md.
IMMUTABLE=0
if command -v steamos-readonly >/dev/null 2>&1 && steamos-readonly status 2>/dev/null | grep -qx enabled; then
  IMMUTABLE=1
elif findmnt -no OPTIONS / 2>/dev/null | grep -qw ro; then
  IMMUTABLE=1
fi
if [[ "$IMMUTABLE" == 1 && "$GRAVE_ROOT" == "/srv/dev" ]]; then
  GRAVE_ROOT="$HOME/gravedecay"
  echo "🪦 immutable rootfs detected — installing under $GRAVE_ROOT (survives OS updates)"
fi
DEST="$GRAVE_ROOT/repos/gravedecay"

# $HOME is user-owned (no sudo); a system path like /srv/dev needs sudo to create.
if [[ "$GRAVE_ROOT" == "$HOME"/* ]]; then
  mkdir -p "$GRAVE_ROOT/repos"
else
  sudo -n true 2>/dev/null || sudo -v || { echo "sudo access required"; exit 1; }
  sudo mkdir -p "$GRAVE_ROOT/repos"
  sudo chown "$(id -un)" "$GRAVE_ROOT" "$GRAVE_ROOT/repos"
fi

if [[ -d "$DEST/.git" ]]; then
  git -C "$DEST" fetch --tags --prune -q origin
else
  git clone -q "$REPO_URL" "$DEST"
fi

if [[ "${GRAVEDECAY_CHANNEL:-release}" == "release" ]]; then
  tag=$(git -C "$DEST" tag -l 'v*' --sort=-v:refname | head -1)
  if [[ -n "$tag" ]]; then
    git -C "$DEST" checkout -q --detach "$tag"
    echo "🪦 installing gravedecay $tag"
  else
    echo "🪦 no release tags yet — installing latest main"
  fi
fi

exec bash "$DEST/raise.sh" "$@"
