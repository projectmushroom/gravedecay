#!/bin/sh
# Export the README/taskbar skull for browsers. Run on macOS (native sips),
# then commit the generated files; appliances need no SVG renderer or Pillow.
set -eu
root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
source="$root/assets/gravedecay-skull.svg"
destination="$root/dashboard/static"
for size in 16 32 180 192 512; do
  sips -s format png -z "$size" "$size" "$source" --out "$destination/icon-$size.png" >/dev/null
done
cp "$destination/icon-512.png" "$root/assets/gravedecay.png"
cp "$destination/icon-16.png" "$root/assets/t3-icon/favicon-16x16.png"
cp "$destination/icon-32.png" "$root/assets/t3-icon/favicon-32x32.png"
cp "$destination/icon-180.png" "$root/assets/t3-icon/apple-touch-icon.png"
python3 - "$root" <<'PY'
from pathlib import Path
import struct
import sys
root = Path(sys.argv[1])
images = [(size, (root / f'dashboard/static/icon-{size}.png').read_bytes()) for size in (16, 32)]
header = struct.pack('<HHH', 0, 1, len(images))
offset = 6 + 16 * len(images)
for size, png in images:
    header += struct.pack('<BBBBHHII', size, size, 0, 0, 1, 32, len(png), offset)
    offset += len(png)
icon = header + b''.join(png for _, png in images)
for path in ('dashboard/static/favicon.ico', 'assets/t3-icon/favicon.ico'):
    (root / path).write_bytes(icon)
PY
