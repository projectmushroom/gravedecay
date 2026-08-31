#!/usr/bin/env bash
# uninstall.sh — the inverse of raise.sh: ./uninstall.sh [--purge] [--yes] [--dry-run]
#
# The teardown lives in `grave uninstall`; this hands off to the installed CLI,
# or to a scratch copy of this checkout's bin/grave when it is already gone
# (without /etc/gravedecay/grave.conf it assumes GRAVE_ROOT=/srv/dev).
# See docs/UNINSTALL.md.
set -euo pipefail
for c in "$(command -v grave 2>/dev/null || true)" /usr/local/bin/grave "$HOME/.local/bin/grave"; do
  [[ -n "$c" && -x "$c" ]] && exec "$c" uninstall "$@"
done
tmp=$(mktemp -d)   # a copy: uninstall deletes the grave it runs from, never this checkout
cp "$(dirname "${BASH_SOURCE[0]}")"/bin/grave "$(dirname "${BASH_SOURCE[0]}")"/bin/grave-workspaces "$tmp/"
exec "$tmp/grave" uninstall "$@"
