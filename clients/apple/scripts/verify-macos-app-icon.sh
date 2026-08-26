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

asset_info=$(mktemp "${TMPDIR:-/tmp}/gravedecay-assets.XXXXXX")
asset_json="$asset_info.json"
asset_plist="$asset_info.plist"
trap 'rm -f "$asset_info" "$asset_json" "$asset_plist"' EXIT
assetutil --info "$resources/Assets.car" >"$asset_info"
{ printf '{"items":'; cat "$asset_info"; printf '}'; } >"$asset_json"
/usr/bin/plutil -convert xml1 -o "$asset_plist" "$asset_json"

entry=0
valid=false
while [ "$entry" -lt 1000 ]; do
  name=$(/usr/libexec/PlistBuddy -c "Print :items:$entry:Name" "$asset_plist" 2>/dev/null || true)
  if [ "$name" = AppIcon ] &&
     [ "$(/usr/libexec/PlistBuddy -c "Print :items:$entry:PixelHeight" "$asset_plist")" = 1024 ] &&
     [ "$(/usr/libexec/PlistBuddy -c "Print :items:$entry:Opaque" "$asset_plist")" = false ]; then
    valid=true
    break
  fi
  entry=$((entry + 1))
done

[ "$valid" = true ] || {
  echo "AppIcon lost its transparent outer silhouette" >&2
  exit 1
}
