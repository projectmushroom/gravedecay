#!/bin/sh
# Brand the T3 Code web UI: gravedecay favicons + the back-to-gravedecay portal
# pill (see t3-portal-snippet.html). T3 is a pinned npm package — updating it
# overwrites dist/client, so rerun this after every `npm update -g t3`.
# Regenerate the PNGs from ../gravedecay.png (imagemagick) if the logo changes.
set -e
ASSETS="$(cd "$(dirname "$0")" && pwd)"
SRC="$ASSETS/t3-icon"
DIST="${1:-$(npm root -g)/t3/dist/client}"
[ -d "$DIST" ] || { echo "t3 dist not found at $DIST"; exit 1; }
for f in favicon.ico favicon-16x16.png favicon-32x32.png apple-touch-icon.png; do
  sudo cp "$SRC/$f" "$DIST/$f"
done
if ! grep -q gravedecay-pill "$DIST/index.html"; then
  sudo python3 -c "
import sys
page, snip = sys.argv[1], sys.argv[2]
html = open(page).read()
html = html.replace('</body>', open(snip).read() + '</body>')
open(page, 'w').write(html)" "$DIST/index.html" "$ASSETS/t3-portal-snippet.html"
  echo "portal pill injected"
fi
sudo systemctl restart t3code 2>/dev/null || true
echo "T3 branded 🪦 — hard-refresh the browser tab (favicons cache hard)"
