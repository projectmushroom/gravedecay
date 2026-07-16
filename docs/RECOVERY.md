# Recovery

## What a backup contains

`grave backup` → `$GRAVE_ROOT/backups/<timestamp>/`:

- `repos/*.bundle` — full git bundles (all refs) of every repo
- `configs/grave-platform.tar.gz` — `$GRAVE_ROOT/{config,docker,docs,scripts}`
- `configs/claude|codex|gemini.tar.gz` — agent CLI configs from `$HOME`
- `configs/t3code-state.tar.gz` — T3 server state (projects, pairings)
- `configs/workspaces.tar.gz` — workspace homes, T3 state, private checkouts
  including dirty/untracked work, and integration configuration
- `volumes/*.tar.gz` — every named docker volume (postgres data, etc.)

Secrets are excluded by default, including provider keys, Linear keys, GitHub
CLI credentials, and Codex auth. Use `grave backup --include-secrets` only for
an encrypted/off-box destination you control. `manifest.json` records the
choice. Without secrets, restored users reauthenticate; grants, MCP config,
state, and dirty work remain recoverable.

Retention: last `BACKUP_KEEP` (default 7). Copy `$GRAVE_ROOT/backups` off-box
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

Doctor enforces the contract: the timer must be enabled and active, and
`.last-verified` must be newer than `BACKUP_MAX_AGE_DAYS` (default 2) — so a
box whose backups silently stopped fails doctor and pages, instead of being
discovered on restore day.

## Restore pieces

```sh
grave restore                     # list backups
grave restore <ts>                # list contents of one
grave restore <ts> repo <name>    # clone bundle → repos/<name>-restored
grave restore <ts> volume <name>  # recreate + fill docker volume (stop stack first)
grave restore <ts> workspaces     # restore workspace trees and dirty work
```

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
