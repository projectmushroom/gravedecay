# Recovery

## What a backup contains

`grave backup` → `$GRAVE_ROOT/backups/<timestamp>/`:

- `repos/*.bundle` — full git bundles (all refs) of every repo
- `configs/grave-platform.tar.gz` — `$GRAVE_ROOT/{config,docker,docs,scripts}`
- `configs/claude|codex|gemini.tar.gz` — agent CLI configs from `$HOME`
- `configs/t3code-state.tar.gz` — T3 server state (projects, pairings)
- `volumes/*.tar.gz` — every named docker volume (postgres data, etc.)

Retention: last `BACKUP_KEEP` (default 7). Schedule it:
`systemd-run --on-calendar=daily` a `grave backup`, or a cron/timer of your
choice. Copy `$GRAVE_ROOT/backups` off-box if the data matters.

## Restore pieces

```sh
grave restore                     # list backups
grave restore <ts>                # list contents of one
grave restore <ts> repo <name>    # clone bundle → repos/<name>-restored
grave restore <ts> volume <name>  # recreate + fill docker volume (stop stack first)
```

## Full box loss → new box

1. Fresh install, clone gravedecay, `./raise.sh --profile <profile>`.
2. Copy the latest backup dir onto the new box.
3. Untar `configs/*` into place (`$GRAVE_ROOT`, `$HOME`), restore volumes,
   clone repo bundles.
4. `sudo systemctl restart t3code gravedash && grave doctor`.
5. Re-pair devices with T3; `tailscale up --ssh` with the same account.

## Btrfs snapshots (if configured)

Hourly snapper timeline on the `$GRAVE_ROOT` subvolume covers oops-level
mistakes: `sudo snapper -c srv list`, `sudo snapper -c srv undochange N..M`.
Snapshots are not backups — they die with the disk.
