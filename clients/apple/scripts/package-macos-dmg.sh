#!/bin/sh
set -eu

# Reproducible-path, unsigned-by-default direct distribution image. Signing is
# performed by Xcode when CODE_SIGN_IDENTITY is supplied to the build.
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
BUILD_DIR=${BUILD_DIR:-"$ROOT/build"}
APP=${APP:-"$ROOT/../DerivedData/Build/Products/Release/Gravedecay.app"}
OUT=${OUT:-"$BUILD_DIR/Gravedecay-macOS.dmg"}
mkdir -p "$BUILD_DIR"
[ -d "$APP" ] || { echo "missing app: $APP" >&2; exit 1; }
"$ROOT/scripts/verify-macos-app-icon.sh" "$APP"
rm -f "$OUT"
STAGE=$(mktemp -d "${TMPDIR:-/tmp}/gravedecay-dmg.XXXXXX")
trap 'rm -rf "$STAGE"' EXIT
cp -R "$APP" "$STAGE/Gravedecay.app"
ln -s /Applications "$STAGE/Applications"
hdiutil create -volname Gravedecay -srcfolder "$STAGE" -ov -format UDZO "$OUT" >/dev/null
if [ -n "${NOTARY_PROFILE:-}" ]; then
  codesign -dv --verbose=2 "$APP" 2>&1 | grep -q 'Authority=Developer ID Application' || { echo 'NOTARY_PROFILE requires a Developer ID-signed app' >&2; exit 1; }
  codesign --verify --deep --strict --verbose=2 "$APP"
  xcrun notarytool submit "$OUT" --keychain-profile "$NOTARY_PROFILE" --wait
  xcrun stapler staple "$OUT"
  xcrun stapler validate "$OUT"
fi
echo "$OUT"
