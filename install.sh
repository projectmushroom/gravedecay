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
DEST="$GRAVE_ROOT/repos/gravedecay"

[[ $EUID -eq 0 ]] && { echo "Run as your normal user (sudo is used internally)."; exit 1; }
command -v git >/dev/null || { echo "Install git first, then re-run."; exit 1; }
sudo -n true 2>/dev/null || sudo -v || { echo "sudo access required"; exit 1; }

sudo mkdir -p "$GRAVE_ROOT/repos"
sudo chown "$(id -un)" "$GRAVE_ROOT" "$GRAVE_ROOT/repos"

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
