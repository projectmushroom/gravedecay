# Choosing the appliance account

On a fresh mutable Linux host, `install.sh` and `raise.sh` offer:

1. `grave` — the recommended dedicated appliance owner (Enter selects it).
2. A custom Unix username.
3. The current user, when it is not root.

The installer reads the choice from `/dev/tty`, including when launched with
`curl | bash`. The owner controls the appliance and is a trusted administrator.
Its home contains GitHub/provider logins and user-local tools. All owner
services and `$GRAVE_ROOT` use that account. The primary group comes from the
Unix account database; it need not have the same name as the user.

```sh
./raise.sh --profile generic --user grave
./raise.sh --profile generic --user devbox
./raise.sh --profile generic --user current
```

Root may start the installer. Selecting root as the appliance owner is refused.
When selecting another account for a fresh install, an administrator creates
it if absent, sets its local sudo password interactively, and stages a clean
copy of the installer at `$GRAVE_ROOT/repos/gravedecay`. The script then enters
that account with `sudo -iu` and continues installation. Use a committed source
checkout so staging preserves the exact installer revision. Existing custom
accounts need a login shell and a home; their passwords are not changed.

Newly selected dedicated owners get a password-required administrator rule in
`/etc/sudoers.d/40-gravedecay-owner`, validated before installation. Setup and
manual maintenance can use their local password. The usual scoped NOPASSWD
rule is installed by the raise for routine dashboard/CLI operations. No blanket
passwordless administrator rule is created. SSH password login remains disabled.

After setup, use the selected account for every login:

```sh
sudo -iu grave                  # substitute your chosen owner
gh auth login
gh auth setup-git
```

The installer does not copy the administrator's GitHub/provider credentials,
SSH keys, shell configuration, or authentication environment into this home.
Finish T3 pairing and provider sign-in from that account's UI/terminal.

## Unattended installs and immutable hosts

A fresh headless mutable-host install requires an explicit `--user` choice.
Pre-provision the named account, its home, and the sudo access needed for the
first raise; account creation cannot set a password without a terminal.
`--user current` is the existing cloud/CI owner-account path.

Immutable hosts retain their existing login account and home-based toolchain;
choosing a different account is refused. This preserves SteamOS update survival
and the existing rootless Docker/Homebrew context. The separate macOS companion
continues to use its logged-in Mac user.

## Existing installations

The choice is recorded in `$GRAVE_ROOT/config/owner`. Re-raises and upgrades
use it without prompting. For older installations without the record, setup
adopts the existing dashboard service user and checks the appliance directory
owner agrees; when no service exists, it adopts the existing data-tree owner.
A directory containing only the bootstrapped gravedecay repository is still
considered fresh.

`--user` refuses to change an established owner. Neither editing the record nor
rerunning under a different account is a migration procedure. Moving an existing
appliance requires coordinated service, data, home/toolchain, and credential
changes with a verified backup; that migration is not implemented here.

Doctor checks the recorded account exists, is non-root, owns the home and data
root, and matches installed owner services. Uninstall removes the generated
administrator rule last, retains the Unix account and its home, and preserves
the owner record with the default retained appliance data.
