#!/bin/sh
set -eu

root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
source="$root/App/Assets.xcassets/AppIcon.appiconset/AppIcon.svg"
destination="$root/App/Assets.xcassets/AppIcon.appiconset"

for size in 16 32 64 128 256 512 1024; do
  sips -s format png -z "$size" "$size" "$source" --out "$destination/icon-$size.png" >/dev/null
done
