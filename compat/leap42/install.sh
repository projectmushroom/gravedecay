#!/usr/bin/env bash
# Opt-in recovery/bootstrap for x86_64 openSUSE Leap 42.3. Run as the owner.
# Usage: compat/leap42/install.sh [--root /srv/dev] [--legacy-seccomp] [--check]
set -euo pipefail
compat_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
repo_dir=$(cd "$compat_dir/../.." && pwd)
grave_root=/srv/dev
legacy_seccomp=0
check_only=0
while (($#)); do
  case "$1" in
    --root) grave_root="$2"; shift 2 ;;
    --legacy-seccomp) legacy_seccomp=1; shift ;;
    --check) check_only=1; shift ;;
    -h|--help) sed -n '2,3p' "$0"; exit 0 ;;
    *) echo "Unknown argument: $1" >&2; exit 1 ;;
  esac
done
if ((check_only)); then exec "$compat_dir/check.sh"; fi
[[ $EUID != 0 ]] || { echo 'Run as the appliance owner; sudo is used internally.' >&2; exit 1; }
# Deliberately narrow: these pinned binaries and exceptions were tested here.
. /etc/os-release
[[ "$ID" == opensuse && "$VERSION_ID" == 42.3 && "$(uname -m)" == x86_64 ]] \
  || { echo 'This installer is only for x86_64 openSUSE Leap 42.3.' >&2; exit 1; }
[[ "$grave_root" == /* && "$grave_root" != / ]] || { echo 'Use an absolute appliance root.' >&2; exit 1; }
if [[ -r /etc/gravedecay/grave.conf ]] && ( . /etc/gravedecay/grave.conf; [[ "${MULTI_USER:-0}" == 1 ]] ); then
  echo 'The legacy bootstrap is for single-user appliances only.' >&2; exit 1
fi
for tool in curl tar xz sha256sum gcc sudo cmp; do
  command -v "$tool" >/dev/null || { echo "Install prerequisite: $tool" >&2; exit 1; }
done
[[ -x /usr/bin/docker ]] || { echo 'Install the distro Docker daemon first.' >&2; exit 1; }
owner=$(id -un)
owner_group=$(id -gn)
export PATH="$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin"
stage=$(mktemp -d)
trap 'rm -rf "$stage"' EXIT
fetch() {
  local url="$1" digest="$2" dest="$3"
  curl -fsSL --connect-timeout 15 --max-time 300 "$url" -o "$dest"
  [[ "$(sha256sum <"$dest" | cut -d' ' -f1)" == "$digest" ]] \
    || { echo "checksum mismatch: $url" >&2; exit 1; }
}
put() {
  local src="$1" dest="$2" mode="${3:-755}"
  if [[ -f "$dest" && ! -L "$dest" ]] && cmp -s "$src" "$dest"; then return; fi
  sudo install -D -m "$mode" "$src" "$dest"
}
# uv's musl build runs independently of the host glibc. Downloads are pinned.
if ! command -v uv >/dev/null; then
  fetch https://github.com/astral-sh/uv/releases/download/0.12.9/uv-x86_64-unknown-linux-musl.tar.gz \
    aa4b1f8770910f7c7c543c7acc980e4270e52e70750c996acef813ea1c7c2912 "$stage/uv.tgz"
  tar -xzf "$stage/uv.tgz" -C "$stage"
  put "$stage/uv-x86_64-unknown-linux-musl/uv" /usr/local/bin/uv
fi
python_env="$HOME/.local/share/gravedecay-python"
if [[ ! -x "$python_env/bin/python" ]]; then
  uv python install 3.11.16
  uv venv --python 3.11.16 "$python_env"
fi
# These wheels run on glibc 2.22; newer cryptography wheels may require 2.28.
uv pip install --python "$python_env/bin/python" --only-binary :all: \
  'pillow==10.4.0' 'cryptography==44.0.3' 'cffi==2.1.1' 'pycparser==3.0'
mkdir -p "$HOME/.local/bin"
for name in python3 python3.11; do
  printf '#!/bin/sh\nexec "%s/bin/python" "$@"\n' "$python_env" >"$stage/python"
  install -m 755 "$stage/python" "$HOME/.local/bin/$name"
done
node_dir=/opt/node-v22.22.2-glibc217
if [[ ! -x "$node_dir/bin/node" ]]; then
  fetch https://unofficial-builds.nodejs.org/download/release/v22.22.2/node-v22.22.2-linux-x64-glibc-217.tar.xz \
    2a15595264fb9bcd7f5e1d64567b3dc31b3c98ab5ea9e4d8cb44e682704af740 "$stage/node.tar.xz"
  tar -xJf "$stage/node.tar.xz" -C "$stage"
  sudo mkdir -p /opt
  sudo mv "$stage/node-v22.22.2-linux-x64-glibc-217" "$node_dir"
fi
for name in node npm npx; do
  # Keep wrappers outside the npm prefix so package upgrades cannot replace them.
  printf '#!/bin/sh\nexec "%s/bin/%s" "$@"\n' "$node_dir" "$name" >"$stage/$name"
  put "$stage/$name" "/usr/local/bin/$name"
done
gcc -shared -fPIC -O2 -Wall -Wextra -o "$stage/compat.so" "$compat_dir/memfd.c"
put "$stage/compat.so" /usr/local/lib/gravedecay-glibc-compat.so 644
if [[ ! -f "$node_dir/lib/node_modules/t3/package.json" ]]; then
  sudo env PATH="$node_dir/bin:/usr/local/bin:/usr/bin:/bin" \
    LD_PRELOAD=/usr/local/lib/gravedecay-glibc-compat.so \
    "$node_dir/bin/npm" install -g --prefix "$node_dir" --allow-scripts=msgpackr-extract,node-pty t3@0.0.38
fi
cat >"$stage/t3" <<'WRAPPER'
#!/bin/sh
# Reapply with compat/leap42/install.sh after replacing /usr/local.
export LD_PRELOAD="/usr/local/lib/gravedecay-glibc-compat.so${LD_PRELOAD:+:$LD_PRELOAD}"
exec /opt/node-v22.22.2-glibc217/bin/t3 "$@"
WRAPPER
put "$stage/t3" /usr/local/bin/t3
# Install only the client; never replace/restart the distro's Docker daemon.
if [[ ! -x /usr/local/libexec/gravedecay/docker-cli ]]; then
  fetch https://download.docker.com/linux/static/stable/x86_64/docker-27.5.1.tgz \
    4f798b3ee1e0140eab5bf30b0edc4e84f4cdb53255a429dc3bbae9524845d640 "$stage/docker.tgz"
  tar -xzf "$stage/docker.tgz" -C "$stage" docker/docker
  put "$stage/docker/docker" /usr/local/libexec/gravedecay/docker-cli
fi
if [[ ! -x /usr/local/lib/docker/cli-plugins/docker-compose ]]; then
  fetch https://github.com/docker/compose/releases/download/v2.30.3/docker-compose-linux-x86_64 \
    fbb4853d3f2148b0f2f0916f8971c9e500784e4e4949324934fc0b7dc2ed5016 "$stage/compose"
  put "$stage/compose" /usr/local/lib/docker/cli-plugins/docker-compose
fi
cat >"$stage/docker" <<'WRAPPER'
#!/bin/sh
if [ "${1:-}" = compose ]; then
    shift
    exec /usr/local/lib/docker/cli-plugins/docker-compose "$@"
fi
exec /usr/local/libexec/gravedecay/docker-cli "$@"
WRAPPER
put "$stage/docker" /usr/local/bin/docker
sudo install -d -o "$owner" -g "$owner_group" "$grave_root/config" "$grave_root/scripts"
install -m 755 "$compat_dir/check.sh" "$grave_root/scripts/leap42-check"
if ((legacy_seccomp)); then
  daemon_version=$(sudo docker version --format '{{.Server.Version}}')
  [[ "$daemon_version" == 18.09.* ]] \
    || { echo 'Legacy seccomp exception is restricted to Docker 18.09.' >&2; exit 1; }
  for stack in core browsers; do
    dest="$grave_root/docker/$stack"
    if [[ ! -d "$dest" ]]; then
      sudo install -d -o "$owner" -g "$owner_group" "$dest"
      cp "$repo_dir/docker/$stack/compose.yaml" "$dest/compose.yaml"
    fi
    # Never overwrite an operator's existing override.
    if [[ -e "$dest/compose.override.yaml" ]] && ! cmp -s "$compat_dir/$stack.override.yaml" "$dest/compose.override.yaml"; then
      echo "Merge $compat_dir/$stack.override.yaml into $dest/compose.override.yaml manually." >&2
      exit 1
    fi
    install -m 644 "$compat_dir/$stack.override.yaml" "$dest/compose.override.yaml"
  done
fi
"$compat_dir/check.sh"
printf 'leap42-v1\n' >"$grave_root/config/leap42-compat"
echo 'Compatibility runtime ready. Run ./raise.sh --profile generic as this user.'
echo 'Existing services and containers have not been restarted.'
