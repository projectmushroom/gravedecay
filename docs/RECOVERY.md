# Recovery

## What a backup contains

`grave backup` → `$GRAVE_ROOT/backups/<timestamp>/`:

- `repos/*.bundle` — full git bundles (all refs) of every repo
- `configs/grave-platform.tar.gz` — `$GRAVE_ROOT/{config,docker,docs,scripts}`
- `configs/claude|codex|gemini.tar.gz` — agent CLI configs from `$HOME`
- `configs/t3code-state.tar.gz` — T3 server state (projects, pairings)
- `configs/workspaces.tar.gz` — workspace homes, T3 state, private checkouts
  including dirty/untracked work, and integration configuration
- `configs/agent-worktrees.tar.gz` — managed agent checkout files (including
  staged, dirty, untracked, and ignored files) and session `meta.json` records;
  `recovery/<session>/` contains a verified commit bundle, HEAD/branch record,
  and binary staged patch. Created privately when `worktrees/` exists.
  Committed branches are also in the source repository's bundle. These are raw
  project files and may include project-local credentials.
- `volumes/*.tar.gz` — every named docker volume (postgres data, etc.)

Secrets are excluded by default, including provider keys, Linear keys, GitHub
CLI credentials, and Codex auth. Use `grave backup --include-secrets` only for
an encrypted/off-box destination you control. `manifest.json` records the
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

Publication is atomic; capturing live source files is not. Stop database writers
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
3. Untar `configs/*` into place (`$GRAVE_ROOT`, `$HOME`), restore volumes,
   clone repo bundles.
4. Run `raise.sh`, then `grave restore <ts> workspaces` when applicable.
5. Run `raise.sh` again to reapply users/units, reauthenticate omitted secrets,
   and require `grave doctor` to pass before changing Serve routing.
6. Re-pair devices with T3; `tailscale up --ssh` with the same account.

## Btrfs snapshots (if configured)

Hourly snapper timeline on the `$GRAVE_ROOT` subvolume covers oops-level
mistakes: `sudo snapper -c srv list`, `sudo snapper -c srv undochange N..M`.
Snapshots are not backups — they die with the disk.
