#!/bin/sh
# Prepare a signed feed beside a verified DMG. Publication remains a release step.
set -eu
DMG=${1:?usage: prepare-appcast.sh DMG vX.Y.Z OUTPUT_DIRECTORY}
TAG=${2:?missing release tag}
DEST=${3:?missing output directory}
: "${SPARKLE_DIST:?path to the checksum-verified Sparkle tools}"
: "${SPARKLE_KEY_FILE:?path to private signing key file (never commit)}"
python3 - "$TAG" <<'PY'
import re, sys
assert re.fullmatch(r'v[0-9]+\.[0-9]+\.[0-9]+', sys.argv[1]), 'Expected vX.Y.Z'
PY
mkdir -p "$DEST"
cp "$DMG" "$DEST/Gravedecay-macOS.dmg"
# Keep prior signed release entries so Settings can offer intermediate versions.
previous=$(gh release view --repo projectmushroom/gravedecay --json tagName --jq .tagName)
if gh release view "$previous" --repo projectmushroom/gravedecay --json assets --jq '.assets[].name' | grep -qx appcast.xml; then
  gh release download "$previous" --repo projectmushroom/gravedecay --pattern appcast.xml --dir "$DEST"
fi
"$SPARKLE_DIST/bin/generate_appcast" --ed-key-file "$SPARKLE_KEY_FILE" \
  --download-url-prefix "https://github.com/projectmushroom/gravedecay/releases/download/$TAG/" \
  --link "https://github.com/projectmushroom/gravedecay/releases/tag/$TAG" \
  --maximum-versions 10 --maximum-deltas 0 "$DEST"
"$SPARKLE_DIST/bin/sign_update" --verify --ed-key-file "$SPARKLE_KEY_FILE" "$DEST/appcast.xml"
python3 - "$DEST/appcast.xml" "$TAG" <<'PY'
import sys, xml.etree.ElementTree as ET
tree = ET.parse(sys.argv[1])
version = tree.findtext('./channel/item/{http://www.andymatuschak.org/xml-namespaces/sparkle}version')
assert version == sys.argv[2][1:], f'App bundle version {version} does not match tag {sys.argv[2]}'
PY
