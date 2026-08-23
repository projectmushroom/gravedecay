#!/usr/bin/env bash
set -euo pipefail
here=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
id=projectmushroom.gravedecay
dest="${XDG_CONFIG_HOME:-$HOME/.config}/omarchy/plugins/$id"
update=false
[[ ${1:-} == --update ]] && update=true
[[ $# -le 1 ]] || { echo "usage: $0 [--update]" >&2; exit 2; }
command -v omarchy >/dev/null || { echo "Omarchy 4/Quattro is required" >&2; exit 1; }
omarchy plugin validate "$here"
if [[ -e $dest ]]; then
  [[ $update == true && -f $dest/manifest.json ]] || { echo "refusing to overwrite $dest (pass --update for this plugin)" >&2; exit 1; }
  grep -qx '  "id": "projectmushroom.gravedecay",' "$dest/manifest.json" || { echo "refusing to overwrite a different plugin" >&2; exit 1; }
fi
mkdir -p "$(dirname "$dest")"
[[ ! -e ${dest}.previous ]] || { echo "refusing to replace existing ${dest}.previous" >&2; exit 1; }
stage=$(mktemp -d "${dest}.stage.XXXXXX")
trap 'rm -rf "$stage"' EXIT
find "$here" -mindepth 1 -maxdepth 1 ! -name README.md ! -name install-local.sh ! -name tests -exec cp -a {} "$stage" \;
if [[ -e $dest ]]; then mv "$dest" "${dest}.previous"; fi
mv "$stage" "$dest"; trap - EXIT
omarchy plugin validate "$dest"
omarchy-shell shell rescanPlugins
omarchy plugin enable "$id" --yes
echo "Installed $id. Previous payload (if any): ${dest}.previous"
