#!/usr/bin/env bash
set -euo pipefail
here=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
id=projectmushroom.gravedecay
plugins="$HOME/.config/omarchy/plugins"
dest="$plugins/$id"
case ${1:-} in
  "") update=false ;;
  --update) update=true ;;
  *) echo "usage: $0 [--update]" >&2; exit 2 ;;
esac
command -v omarchy >/dev/null || { echo "Omarchy 4/Quattro is required" >&2; exit 1; }
omarchy plugin validate "$here"
if [[ -e $dest ]]; then
  [[ $update == true && -f $dest/manifest.json ]] || { echo "refusing to overwrite $dest (pass --update for this plugin)" >&2; exit 1; }
  existing=$(python3 - "$dest/manifest.json" <<'PY'
import json, sys
try: print(json.load(open(sys.argv[1])).get("id", ""))
except (OSError, ValueError): pass
PY
)
  [[ $existing == "$id" ]] || { echo "refusing to overwrite a different plugin" >&2; exit 1; }
fi
mkdir -p "$plugins"
stage=$(mktemp -d "$plugins/.${id}.stage.XXXXXX")
backup=""
installed=false
rollback() {
  if [[ -n $backup && -e $backup ]]; then
    rm -rf "$dest"
    mv "$backup" "$dest"
  elif [[ $installed == true ]]; then
    rm -rf "$dest"
  fi
}
trap 'rollback; rm -rf "$stage"' ERR
find "$here" -mindepth 1 -maxdepth 1 ! -name README.md ! -name install-local.sh ! -name tests -exec cp -a {} "$stage" \;
if [[ -e $dest ]]; then
  backup=$(mktemp -d "$plugins/.${id}.backup.XXXXXX")
  rmdir "$backup"
  mv "$dest" "$backup"
fi
mv "$stage" "$dest"
installed=true
omarchy plugin validate "$dest"
omarchy-shell shell rescanPlugins
omarchy plugin enable "$id"
rm -rf "$backup"
trap - ERR
echo "Installed $id"
