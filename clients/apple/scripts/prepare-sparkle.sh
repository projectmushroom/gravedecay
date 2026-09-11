#!/bin/sh
set -eu
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
DEST="$ROOT/build/sparkle-2.9.6"
ARCHIVE="$ROOT/build/Sparkle-2.9.6.tar.xz"
mkdir -p "$ROOT/build"
if [ ! -f "$ARCHIVE" ]; then
  curl --fail --location --retry 3 --output "$ARCHIVE" https://github.com/sparkle-project/Sparkle/releases/download/2.9.6/Sparkle-2.9.6.tar.xz
fi
printf '%s  %s\n' 52bf9e88cdd972fc0c81501377a880e90d47031bd8ca5462488f843e2609e192 "$ARCHIVE" | shasum -a 256 -c - >/dev/null
if [ ! -x "$DEST/bin/generate_appcast" ]; then
  mkdir -p "$DEST"
  tar -xf "$ARCHIVE" -C "$DEST"
fi
echo "$DEST"
