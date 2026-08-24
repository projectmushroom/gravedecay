#!/bin/sh
set -eu

# Deterministic, unsigned-by-default direct distribution image.  Signing is
# opt-in: set CODE_SIGN_IDENTITY and NOTARY_PROFILE in the release environment.
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
BUILD_DIR=${BUILD_DIR:-"$ROOT/build"}
APP=${APP:-"$ROOT/../DerivedData/Build/Products/Release/Gravedecay.app"}
OUT=${OUT:-"$BUILD_DIR/Gravedecay-macOS.dmg"}
mkdir -p "$BUILD_DIR"
[ -d "$APP" ] || { echo "missing app: $APP" >&2; exit 1; }
rm -f "$OUT"
STAGE=$(mktemp -d "${TMPDIR:-/tmp}/gravedecay-dmg.XXXXXX")
trap 'rm -rf "$STAGE"' EXIT
cp -R "$APP" "$STAGE/Gravedecay.app"
ln -s /Applications "$STAGE/Applications"
hdiutil create -volname Gravedecay -srcfolder "$STAGE" -ov -format UDZO "$OUT" >/dev/null
if [ -n "${NOTARY_PROFILE:-}" ]; then
  xcrun notarytool submit "$OUT" --keychain-profile "$NOTARY_PROFILE" --wait
  xcrun stapler staple "$OUT"
fi
echo "$OUT"
