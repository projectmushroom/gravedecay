#!/bin/sh
set -eu

app=${1:?usage: verify-macos-app-icon.sh path/to/Gravedecay.app}
resources="$app/Contents/Resources"
info="$app/Contents/Info.plist"

[ -f "$resources/AppIcon.icns" ] || { echo "missing AppIcon.icns" >&2; exit 1; }
[ "$(/usr/libexec/PlistBuddy -c 'Print :CFBundleIconFile' "$info")" = AppIcon ] || {
  echo "CFBundleIconFile is not AppIcon" >&2
  exit 1
}

assetutil --info "$resources/Assets.car" | grep -q '"Name" : "AppIcon"' || {
  echo "Assets.car has no AppIcon" >&2
  exit 1
}
assetutil --info "$resources/Assets.car" | grep -q '"PixelHeight" : 1024' || {
  echo "Assets.car has no 1024px AppIcon rendition" >&2
  exit 1
}
assetutil --info "$resources/Assets.car" | grep -q '"Opaque" : false' || {
  echo "AppIcon lost its transparent outer silhouette" >&2
  exit 1
}
