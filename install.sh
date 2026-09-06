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
install_args=("$@")
while (($#)); do
  case "$1" in
    --root) GRAVE_ROOT="${2:?--root requires a path}"; shift 2 ;;
    --profile|--user) [[ $# -ge 2 ]] || { echo "$1 requires a value"; exit 1; }; shift 2 ;;
    *) shift ;;
  esac
done
[[ "$GRAVE_ROOT" == /* && "$GRAVE_ROOT" != / ]] || { echo "--root must be an absolute appliance directory, not /"; exit 1; }
admin() { if [[ $EUID == 0 ]]; then "$@"; else sudo "$@"; fi; }

if ! command -v git >/dev/null || ! command -v sudo >/dev/null; then
  echo "🪦 installing Git and sudo prerequisites"
  if command -v dnf >/dev/null; then
    admin dnf install -y git sudo
  elif command -v apt-get >/dev/null; then
    admin apt-get update -qq && admin apt-get install -y git sudo
  elif command -v pacman >/dev/null; then
    admin pacman -S --needed --noconfirm git sudo
  elif command -v zypper >/dev/null; then
    admin zypper --non-interactive install git sudo
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
BOOTSTRAP_OWNER="$(id -un)"
[[ ! -d "$GRAVE_ROOT" ]] || BOOTSTRAP_OWNER="$(stat -c %U "$GRAVE_ROOT")"
bootstrap_git() {
  if [[ "$BOOTSTRAP_OWNER" == "$(id -un)" ]]; then git "$@";
  else sudo -H -u "$BOOTSTRAP_OWNER" git "$@"; fi
}

# $HOME is user-owned (no sudo); a system path like /srv/dev needs sudo to create.
if [[ "$GRAVE_ROOT" == "$HOME"/* ]]; then
  mkdir -p "$GRAVE_ROOT/repos"
else
  [[ $EUID == 0 ]] || sudo -n true 2>/dev/null || sudo -v || { echo "sudo access required"; exit 1; }
  admin mkdir -p "$GRAVE_ROOT/repos"
  admin chown "$BOOTSTRAP_OWNER" "$GRAVE_ROOT" "$GRAVE_ROOT/repos"
fi

if [[ -d "$DEST/.git" ]]; then
  [[ -z "$(bootstrap_git -C "$DEST" status --porcelain)" ]] || { echo "checkout has local changes; commit or stash them before installing"; exit 1; }
  bootstrap_git -C "$DEST" fetch --tags --prune -q origin
else
  bootstrap_git clone -q "$REPO_URL" "$DEST"
fi

if [[ "${GRAVEDECAY_CHANNEL:-release}" == "release" ]]; then
  tag=$(bootstrap_git -C "$DEST" tag -l 'v*' --sort=-v:refname | head -1)
  if [[ -n "$tag" ]]; then
    bootstrap_git -C "$DEST" checkout -q --detach "$tag"
    echo "🪦 installing gravedecay $tag"
  else
    echo "🪦 no release tags yet — installing latest main"
  fi
fi

exec bash "$DEST/raise.sh" --root "$GRAVE_ROOT" "${install_args[@]}"
