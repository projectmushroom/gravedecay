# Recovery

## What a backup contains

`grave backup` → `$GRAVE_ROOT/backups/<timestamp>/`:

- `repos/*.bundle` — full git bundles (all refs) of every repo
- `configs/grave-platform.tar.gz` — `$GRAVE_ROOT/{config,docker,docs,scripts}`,
  excluding regenerated service capabilities, private recovery staging, and the
  workspace registry (captured with the workspace data below)
- `configs/claude|codex|gemini.tar.gz` — agent CLI configs from `$HOME`
- `configs/t3code-state.tar.gz` — T3 server state (projects, pairings)
- `configs/workspaces.tar.gz` — workspace homes, T3 state, private checkouts
  including dirty/untracked work, integration configuration, and the registry
  snapshot as its first member, `config/workspaces.json`. With `--include-secrets`,
  this archive also carries `config/secrets/provider.env` when configured
- `configs/agent-worktrees.tar.gz` — managed agent checkout files (including
  staged, dirty, untracked, and ignored files) and session `meta.json` records;
  `recovery/<session>/` contains a verified commit bundle, HEAD/branch record,
  and binary staged patch. Created privately when `worktrees/` exists.
  Committed branches are also in the source repository's bundle. These are raw
  project files and may include project-local credentials.
- `volumes/*.tar.gz` — every named docker volume (postgres data, etc.)

Known credential locations are excluded by default, including provider keys,
workspace `config/secrets`, GitHub CLI credentials in both `config/gh` and
`.config/gh`, Codex auth, and Claude credential files. Project-local credentials
and unrecognized copies remain ordinary project files; this is not a content
scanner. Gateway/admin capabilities and root workspace service environments are
never archived, even with `--include-secrets`; provisioning regenerates them.
Use `grave backup --include-secrets` only for an encrypted/off-box destination
you control. `manifest.json` records the
choice. Without secrets, restored users reauthenticate; grants, MCP config,
state, and dirty work remain recoverable.

Retention: last `BACKUP_KEEP` completed backups (default 7; must be a positive
integer). Failed runs never prune older backups. Copy `$GRAVE_ROOT/backups` off-box
if the data matters — snapshots and on-box backups die with the disk.

## Scheduling and verification

raise.sh installs and enables `gravedecay-backup.timer`: a `grave backup`
every night around 05:00 (randomized ±20 min, `Persistent=true` so a box that
slept through the night catches up at wake/boot).

Every artifact is verified as it is written — git bundles with
`git bundle verify`, tarballs by listing them back — and any failure makes the
whole run exit nonzero, which lands `gravedecay-backup.service` in failed
state and pages via `gravedecay-notify@` (when notifications are configured,
docs/NOTIFICATIONS.md). Only a fully clean run updates
`$BACKUP_DIR/.last-verified`.

New backups also record a version 2 `manifest.json` containing a SHA-256 digest
and byte size for every artifact. The run holds a nonblocking lock and writes
into a private `.incomplete-<timestamp>.<random>/` directory. Only after every
artifact succeeds and the inventory is recorded is that directory renamed to
`<timestamp>/`. A second simultaneous backup fails immediately; a repeated
timestamp is refused rather than overwritten. Interrupted or failed staging
directories remain for inspection and are excluded from retention; remove them
manually when no longer needed. New backup directories are mode 700. When root
creates a backup (including during multi-user migration), completed artifacts,
freshness markers, and the run lock belong to the appliance owner, determined
by `$GRAVE_ROOT` ownership. Doctor and the next owner-run backup retain access;
workspace collaborators do not gain access.

The nightly service continues to run as the appliance owner. Docker volume
archives stream from the container into a file opened by that owner with mode
600, so container root ownership or umask cannot leak into backup artifacts. For workspace data,
it opens the private destination and invokes the existing scoped root helper
`grave __users backup-export`. That helper accepts no destination pathname and
streams one archive on stdout. It holds the registry lock and launches a fresh
process for each home under that workspace's UID, with no supplementary groups
or inherited administrator environment. Traversal uses directory handles and
never follows symlinks. Ordinary links are preserved as links, regular files
(including hardlinks) are copied independently, and live Unix sockets are omitted.
Other special files, unreadable files, missing/unregistered homes, detected file
changes, or an unfinished lifecycle operation fail the backup. A failed export
cannot publish a backup or refresh freshness markers. Pause writers for a
consistent checkpoint; this is not a filesystem snapshot.

The manifest records the covered workspace IDs/slugs and a digest of the exact
registry snapshot. Doctor compares that digest with the current registry, in
addition to checking artifact inventory and backup age. A missing workspace
archive, an older backup without coverage, or a registry change requires a new
`grave backup`. Disabled workspaces are included too. Existing private home
permissions and group memberships are unchanged; collaborators cannot access
the owner's backup directory.

In multi-user mode, migration and reapply also repair existing backup directory
and artifact permissions/ownership, including a custom `BACKUP_DIR`. To repair
again, run `grave users owner-privacy`; `grave users owner-privacy --check` verifies
without changes. Artifacts remain byte-for-byte unchanged, and the repair does
not claim checksum verification or advance backup freshness. Unsafe links,
foreign owners, or shared-writable directories require inspection; an active
backup requires retrying after it finishes. Retained workspace homes under
`backups/removed-workspaces` keep their root-private archive boundary and original
UIDs. See [MULTIUSER.md](MULTIUSER.md#owner-data-and-existing-backups).

After copying a backup, or before relying on it for recovery, run:

```sh
grave backup verify 20260913-010101
```

This reads every artifact and checks its size and checksum, rejecting missing,
unexpected, symlinked, or damaged files. It needs no original Git checkout,
running Docker daemon, or sudo access. It never extracts files or refreshes
backup freshness. Checksums detect accidental damage; they do not authenticate
a backup whose manifest has also been replaced. Only restore trusted backups.

Doctor enforces the contract: the timer must be enabled and active, and
`.last-verified` must be newer than `BACKUP_MAX_AGE_DAYS` (default 2) — so a
box whose backups silently stopped fails doctor and pages, instead of being
discovered on restore day.

For new backups, `.last-backup` records the completed directory name. Doctor
checks that directory's inventory and sizes as well as the existing freshness
check. This catches deleted or truncated backups without reading all database
volumes on every doctor run. Same-size corruption requires `grave backup verify`.
Older installations without the pointer get a nudge to create a new backup.

## Restore pieces

```sh
grave restore                     # list backups
grave restore <ts>                # list contents of one
grave restore <ts> repo <name>    # clone bundle → repos/<name>-restored
grave restore <ts> volume <name>  # recreate + fill docker volume (stop stack first)
grave restore <ts> workspaces     # restore workspace trees and dirty work
```

Every restore operation first verifies all checksums in a version 2 backup,
before cloning repositories, creating volumes, or extracting workspace data.
Legacy version 1 backups remain restorable with a warning; explicit
`grave backup verify` fails for them because they have no checksum inventory.
Listing backup contents does not verify or restore them.

Workspace restore uses the registry embedded in `configs/workspaces.tar.gz`.
For older backups it falls back to the registry in `configs/grave-platform.tar.gz`.
It never extracts over live homes or installs archived scripts/service
credentials. An explicitly archived shared-provider file remains a manual
administrator recovery artifact; workspace restore does not install or grant it. The root helper snapshots these inputs
under `config/workspace-restores/<random>/`, then a fresh process running as the
appliance owner verifies the captured checksums, validates all archive paths,
and extracts into private staging. Version 1 archives get the same path checks
with an integrity warning. Missing registries/homes, duplicate paths, traversal,
special files, and children beneath a link are rejected. Repository symlinks are
preserved without following them; hardlinks within a workspace become independent
regular copies. Archived UIDs, GIDs, privileged modes and metadata are ignored;
new files/directories are private, with ordinary executable bits retained.

All restored IDs, slugs and service ports must be disjoint from the live registry.
Their homes, Unix accounts and service configuration must not already exist.
The helper creates new Unix identities, publishes each home by no-replace rename,
merges the records without replacing existing ones, and generates fresh service
capabilities. Staging and homes must share a filesystem. Services stay stopped
until reapply; restore does not enable multi-user routing or install shared
provider keys. Reauthenticate omitted credentials and configure any required
shared provider before requiring doctor to pass.

Failures retain the input snapshots, remaining staged homes, and a private
receipt. Normal backups exclude restore and migration staging, even with
`--include-secrets`; retain those failed-operation copies separately during
administrator recovery. Publication of several homes is not one atomic transaction: an
interruption may leave some new accounts/homes published before the registry is
saved. Doctor reports pending restore even without a registry; add, migration,
reapply and another restore refuse to continue. Inspect the receipt, registry,
accounts and homes together as administrator, and retain the recovery data before
clearing its staging entry. Retrying never overlays a published home. Existing
workspace data and records are not rolled back or deleted automatically.

Backup publication is atomic; capturing live source files is not. Stop database writers
and pause agents when you need a consistent checkpoint. Checksums prove the
captured bytes survived storage and copying, not application-level consistency.

## Recovering isolated agent work

Extract `configs/agent-worktrees.tar.gz` into a scratch directory first. The
saved `.git` files and session paths refer to the original host; do not use
them to launch sessions on a replacement box. Restore the source repository
bundle, import `recovery/<session>/commits.bundle` if needed, and create a new
worktree at the commit in `recovery/<session>/head.json`. Apply a nonempty
`staged.patch` there with `git apply --index`, then copy the saved checkout
files into it **excluding `.git`**. Apply tracked-file deletions from the saved
tree as well (compare the trees before copying). Applying the staged patch
before copying preserves both versions when a file has staged and unstaged
changes. Inspect `git status` before starting a new session.

This archive supplements the repo bundles; it does not make an atomic snapshot
of a running agent. Pause writing agents when a consistent checkpoint matters.

## Full box loss → new box

1. Fresh install, clone gravedecay, `./raise.sh --profile <profile>`.
2. Copy the latest backup dir onto the new box.
3. Verify the backup, restore volumes and clone repo bundles. Inspect other
   configuration archives in scratch storage before selectively restoring them;
   do not unpack a saved workspace registry, service credentials, migration/restore
   receipts, or workspace homes over the fresh installation.
4. Run `grave restore <ts> workspaces` when applicable, before creating replacement
   users or running owner migration. The restore merges only nonconflicting
   identities/ports into the live registry and creates fresh homes/accounts.
5. Reauthenticate omitted secrets and configure shared provider access if needed.
   For a recovered multi-user appliance, set `MULTI_USER=1` in
   `/etc/gravedecay/grave.conf` and rerun `raise.sh` to install the boundary and
   start the restored services. Require `grave doctor` to pass before use.
6. Re-pair devices with T3; `tailscale up --ssh` with the same account.

## Btrfs snapshots (if configured)

Hourly snapper timeline on the `$GRAVE_ROOT` subvolume covers oops-level
mistakes: `sudo snapper -c srv list`, `sudo snapper -c srv undochange N..M`.
Snapshots are not backups — they die with the disk.

## Retained removed workspaces

`grave users remove <workspace> --confirm <workspace>` preserves the home at the
printed `backups/removed-workspaces/<workspace>-<random>/home` path. Root-owned
mode-700 parent directories protect it even if its original UID is reassigned.
Use administrator/root access to inspect or recover these archives; the ordinary
backup retention timer excludes `removed-workspaces`.

If removal fails after moving the home, leave the registry receipt intact and
rerun the same confirmed remove command after resolving the reported failure
(for example, remaining processes preventing `userdel`). `grave users status`
reports `removal_pending`; doctor rejects unfinished removal. Do not recreate a
home or copy an archive over an existing workspace to bypass the receipt. The
command checks directory identity on retry and refuses conflicting data. Removal
requires a same-filesystem rename and never falls back to recursively copying
collaborator-controlled contents as root.

### Interrupted owner migration

`grave multiuser enable` leaves original single-user data intact. Failed owner
copy/publication leaves a root-private receipt under
`$GRAVE_ROOT/config/workspace-migrations`; `grave doctor` reports it even if no
workspace registry was published. Inspect that staging directory as root and
retain its contents before clearing the receipt. Check the registry, Unix
account, and workspace home together: later failures may have published some
of them. Retry only with a fresh identity/slug/account/home, never by overlaying
a retained workspace. See [migration](MULTIUSER.md#migration) for the boundary
and same-filesystem requirement.
