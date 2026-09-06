#!/usr/bin/env bash
# Shared owner selection for raise.sh. Executed with "prepare" only by the
# installing administrator to create a fresh owner and stage its checkout.

owner_error() { printf 'gravedecay: %s\n' "$*" >&2; return 1; }
owner_valid_name() { [[ "$1" =~ ^[a-z_][a-z0-9_-]{0,31}$ && "$1" != root ]]; }
owner_has_tty() { ( : </dev/tty ) 2>/dev/null; }
owner_read() { IFS= read -r "$1" </dev/tty; }
owner_prompt() { printf "$@" >/dev/tty; }
owner_set_password() { passwd "$1" </dev/tty >/dev/tty; }

owner_bootstrap_tree() {
  # install.sh may already have downloaded just this repository. It is still
  # a fresh install; existing projects/configuration are an adoption instead.
  local root="$1" entry
  [[ -d "$root/repos/gravedecay/.git" && ! -L "$root" && ! -L "$root/repos" && ! -L "$root/repos/gravedecay" ]] || return 1
  while IFS= read -r entry; do
    [[ "$entry" == repos ]] || return 1
  done < <(find "$root" -mindepth 1 -maxdepth 1 -printf '%f\n')
  while IFS= read -r entry; do
    [[ "$entry" == gravedecay ]] || return 1
  done < <(find "$root/repos" -mindepth 1 -maxdepth 1 -printf '%f\n')
}

owner_existing() {
  local root="$1" recorded="" service="" tree=""
  if [[ -e "$root/config/owner" ]]; then
    [[ ! -L "$root/config/owner" ]] || { owner_error "owner record must not be a symlink"; return 1; }
    recorded=$(cat "$root/config/owner") || return 1
    owner_valid_name "$recorded" || { owner_error "invalid recorded appliance owner"; return 1; }
  fi
  service=$(systemctl show gravedecay.service -p User 2>/dev/null | sed -n 's/^User=//p') || service=""
  if [[ -d "$root" ]] && ! owner_bootstrap_tree "$root"; then
    tree=$(stat -c %U "$root") || return 1
  fi
  if [[ -n "$recorded" ]]; then
    [[ -z "$tree" || "$tree" == "$recorded" ]] || {
      owner_error "recorded owner $recorded disagrees with $root ownership ($tree); repair ownership first"; return 1;
    }
    printf '%s\n' "$recorded"
  elif [[ -n "$service" ]]; then
    [[ -z "$tree" || "$tree" == "$service" ]] || {
      owner_error "service owner $service disagrees with $root ownership ($tree); explicit migration required"; return 1;
    }
    printf '%s\n' "$service"
  elif [[ -n "$tree" ]]; then
    printf '%s\n' "$tree"
  fi
}

owner_choose() {
  local requested="$1" current="$2" existing="$3" immutable="$4" answer=""
  [[ "$requested" != current ]] || requested="$current"
  if [[ -n "$existing" ]]; then
    [[ -z "$requested" || "$requested" == "$existing" ]] || {
      owner_error "this appliance belongs to $existing; --user cannot migrate an existing installation"; return 1;
    }
    OWNER_SELECTED="$existing"
  elif [[ -n "$requested" ]]; then
    OWNER_SELECTED="$requested"
  elif [[ "$immutable" == 1 ]]; then
    printf 'Immutable host: retaining the existing login account %s.\n' "$current" >&2
    OWNER_SELECTED="$current"
  else
    owner_has_tty || {
      owner_error "fresh unattended install needs --user grave, --user <name>, or --user current"; return 1;
    }
    owner_prompt '\nAppliance Unix account (services, repositories, and agent logins):\n  1) grave (recommended)\n  2) custom username\n'
    [[ "$current" == root ]] || owner_prompt '  3) current user (%s)\n' "$current"
    owner_prompt 'Choose [1]: '
    owner_read answer || return 1
    case "$answer" in
      ''|1) OWNER_SELECTED=grave ;;
      2) owner_prompt 'Username: '; owner_read OWNER_SELECTED || return 1 ;;
      3) OWNER_SELECTED="$current" ;;
      *) owner_error "choose 1, 2, or 3, or pass --user explicitly"; return 1 ;;
    esac
  fi
  owner_valid_name "$OWNER_SELECTED" || { owner_error "owner must be a non-root Unix username (lowercase letters, digits, '_' or '-')"; return 1; }
  [[ "$immutable" != 1 || "$OWNER_SELECTED" == "$current" ]] || {
    owner_error "immutable hosts use their existing login and home toolchain; use --user current"; return 1;
  }
}

owner_prepare() {
  local owner="$1" root="$2" source="$3" entry home group canonical sudoers_tmp origin revision uid uid_min
  [[ $EUID == 0 ]] || { owner_error "account provisioning requires the installing administrator"; return 1; }
  owner_valid_name "$owner" || return 1
  [[ "$root" == /* && "$root" != / && "$root" != *$'\n'* && ! -L "$root" ]] || return 1
  [[ "$(readlink -m "$root")" == "$root" ]] || { owner_error "use a canonical appliance root without symlink parents"; return 1; }
  [[ ! -e /etc/gravedecay/grave.conf ]] || { owner_error "existing installation requires an owner migration"; return 1; }
  [[ ! -e "$root" ]] || owner_bootstrap_tree "$root" || {
    owner_error "refusing to change ownership of existing appliance data"; return 1;
  }
  # Validate source BEFORE creating a Unix user or changing ownership. A clean
  # commit can be cloned exactly; uncommitted local edits must not disappear.
  [[ -d "$source/.git" ]] || { owner_error "owner setup requires a primary Git checkout"; return 1; }
  [[ -z "$(git -c safe.directory="$source" -C "$source" status --porcelain --untracked-files=all)" ]] || {
    owner_error "commit local installer changes before selecting a different owner"; return 1;
  }
  revision=$(git -c safe.directory="$source" -C "$source" rev-parse HEAD) || return 1
  origin=$(git -c safe.directory="$source" -C "$source" remote get-url origin) || return 1
  canonical="$root/repos/gravedecay"
  if [[ "$source" != "$canonical" && ( -e "$canonical" || -L "$canonical" ) ]]; then
    owner_error "canonical checkout already exists; run its installer"; return 1
  fi
  if ! entry=$(getent passwd "$owner"); then
    owner_has_tty || { owner_error "creating $owner needs a terminal to set its sudo password; unattended installs must pre-provision the account"; return 1; }
    useradd --create-home --shell /bin/bash --comment 'gravedecay appliance owner' "$owner" || return 1
    printf 'Set the local sudo password for %s (SSH password login remains disabled).\n' "$owner"
    owner_set_password "$owner" || return 1
    entry=$(getent passwd "$owner") || return 1
  fi
  home=$(cut -d: -f6 <<<"$entry")
  uid=$(cut -d: -f3 <<<"$entry")
  uid_min=$(awk '$1 == "UID_MIN" {print $2; exit}' /etc/login.defs 2>/dev/null) || uid_min=""
  [[ "$uid_min" =~ ^[0-9]+$ ]] || uid_min=1000
  [[ "$uid" =~ ^[0-9]+$ && "$uid" -ge "$uid_min" && "$uid" != 0 ]] || { owner_error "select a regular login account, not a system account"; return 1; }
  [[ "$home" == /* && "$home" != / && -d "$home" && "$(stat -c %U "$home")" == "$owner" ]] || {
    owner_error "selected account needs a home directory it owns"; return 1;
  }
  case "$(cut -d: -f7 <<<"$entry")" in */nologin|*/false|'') owner_error "selected owner needs a login shell"; return 1 ;; esac
  group=$(id -gn "$owner") || return 1
  # Ordinary password-authenticated administrator access supports the initial
  # raise and later manual maintenance. Never grant blanket NOPASSWD access.
  if [[ -e /etc/sudoers.d/40-gravedecay-owner ]] &&
     ! grep -qxF "$owner ALL=(root) ALL" /etc/sudoers.d/40-gravedecay-owner; then
    owner_error "a different owner administrator rule already exists; inspect it before continuing"; return 1
  fi
  sudoers_tmp=$(mktemp) || return 1
  printf '%s ALL=(root) ALL\n' "$owner" >"$sudoers_tmp"
  chmod 440 "$sudoers_tmp"
  if ! visudo -c -f "$sudoers_tmp" >/dev/null; then rm -f "$sudoers_tmp"; return 1; fi
  install -m 440 -o root -g root "$sudoers_tmp" /etc/sudoers.d/40-gravedecay-owner
  rm -f "$sudoers_tmp"
  if [[ "$source" != "$canonical" ]]; then
    install -d -o "$owner" -g "$group" "$root" "$root/repos"
    git -c safe.directory="$source" clone --no-hardlinks "$source" "$canonical" || return 1
    git -C "$canonical" checkout --detach "$revision" || return 1
    git -C "$canonical" remote set-url origin "$origin"
  fi
  chown "$owner:$group" "$root" "$root/repos"
  chown -R "$owner:$group" "$canonical"
  install -d -o "$owner" -g "$group" "$root/config"
  printf '%s\n' "$owner" >"$root/config/owner"
  chown "$owner:$group" "$root/config/owner"
  chmod 644 "$root/config/owner"
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  set -euo pipefail
  [[ "${1:-}" == prepare && $# == 4 ]] || exit 2
  shift
  owner_prepare "$@"
fi
