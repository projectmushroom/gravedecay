#!/bin/sh
# Brand the T3 Code web UI with the gravedecay tombstone favicon.
# T3 is a pinned npm package — updating it overwrites dist/client, so rerun
# this after every `npm update -g t3`. Regenerate the PNGs from
# ../gravedecay.svg (rsvg-convert + imagemagick) if the logo changes.
set -e
SRC="$(cd "$(dirname "$0")/t3-icon" && pwd)"
DIST="${1:-$(npm root -g)/t3/dist/client}"
[ -d "$DIST" ] || { echo "t3 dist not found at $DIST"; exit 1; }
for f in favicon.ico favicon-16x16.png favicon-32x32.png apple-touch-icon.png; do
  sudo cp "$SRC/$f" "$DIST/$f"
done
sudo systemctl restart t3code 2>/dev/null || true
echo "T3 rebranded 🪦 — hard-refresh the browser tab (favicons cache hard)"
