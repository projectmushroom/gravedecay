#!/usr/bin/env bash
# Installed scheduler smoke; only the disposable CI account gets this fake CLI.
set -euo pipefail
source /etc/gravedecay/grave.conf
fake="$HOME/.local/bin/codex"
[[ ! -e "$fake" ]] || { echo "refusing to overwrite an existing fixture CLI"; exit 1; }
mkdir -p "$(dirname "$fake")"
prompt=$(mktemp)
trap 'rm -f "$fake" "$prompt"' EXIT
cat >"$fake" <<'AGENT'
#!/usr/bin/env bash
set -euo pipefail
[[ "$1" == exec ]]
cat >/dev/null
printf 'scheduled fixture result\n' > scheduled-result.txt
printf 'scheduled provider fixture finished\n'
AGENT
chmod 755 "$fake"
repo="$GRAVE_ROOT/repos/scheduled-smoke"
mkdir "$repo"
git -C "$repo" init -q
git -C "$repo" config user.name 'Scheduler fixture'
git -C "$repo" config user.email 'fixture@example.test'
printf 'base\n' >"$repo/base.txt"
git -C "$repo" add base.txt
git -C "$repo" commit -qm 'fixture base'
printf 'Exercise the fake scheduled provider.\n' >"$prompt"
grave agents run ci-schedule --repo scheduled-smoke --prompt-file "$prompt"
record=""
for _ in $(seq 1 45); do
  for file in "$GRAVE_ROOT/config/secrets/agent-jobs/ci-schedule/runs/"*.json; do
    [[ -f "$file" ]] || continue
    state=$(jq -r .status "$file")
    if [[ "$state" != running ]]; then record="$file"; break; fi
  done
  [[ -z "$record" ]] || break
  sleep 1
done
[[ -n "$record" ]] || { echo 'scheduler never produced a result'; exit 1; }
jq -e '.status == "succeeded" and .exit_code == 0' "$record"
worktree=$(jq -r .dir "$record")
[[ -f "$worktree/scheduled-result.txt" && ! -e "$repo/scheduled-result.txt" ]]
grave agents jobs cancel ci-schedule
grave doctor
