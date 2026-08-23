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
  existing=$(jq -r '.id // empty' "$dest/manifest.json" 2>/dev/null || true)
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
  [[ $installed == true ]] && omarchy-shell shell rescanPlugins >/dev/null 2>&1 || true
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
discovered=0
for (( attempt = 0; attempt < 40; attempt++ )); do
  if omarchy plugin list --json | jq -e --arg id "$id" 'any(.[]; .id == $id)' >/dev/null; then
    discovered=1
    break
  fi
  sleep 0.05
done
if (( ! discovered )); then
  echo "plugin '$id' is not known after rescan" >&2
  rollback
  rm -rf "$stage"
  trap - ERR
  exit 1
fi
omarchy plugin enable "$id"
rm -rf "$backup"
trap - ERR
echo "Installed $id"
