#!/usr/bin/env bash
# tests/e2e/smoke.sh — raise a REAL box in CI (#85).
#
# The unit suite is contract-greps and in-process endpoint tests; every one
# was green while v0.7.0→v0.7.4 each shipped a provisioning failure only a
# real host could reveal (fs.protected_regular vs the sudoers temp file, the
# wrong python behind the units, headless sudo — twice). This harness is the
# missing field test:
#
#   phase 1  first raise on a fresh Arch systemd container (bootstrap sudo
#            stands in for the human at the keyboard)
#   phase 2  the human leaves: bootstrap grant deleted; wheel requires a
#            password nobody can type — the SteamOS shape
#   phase 3  headless re-raise (stdin /dev/null) — the exact environment of
#            gravedecay-upgrade.service; must complete on the raise-installed
#            scoped NOPASSWD grant alone (#89)
#   phase 4  stampless headless re-raise — the pre-stamp sudoers fallback
#            that broke in the field (#96)
#   phase 5  grave doctor green + every service answering
#
# tailscale and t3 are stubbed (CI has no tailnet or provider auth); systemd,
# docker + the compose stacks, sshd, sudoers, and doctor are real.
set -euo pipefail
cd "$(dirname "$0")/../.."
CTR=gravedecay-e2e

cleanup() {
  rc=$?
  if (( rc != 0 )); then
    echo "=== smoke FAILED (rc=$rc) — container journal tail ==="
    docker exec "$CTR" journalctl -n 100 --no-pager 2>/dev/null || true
  fi
  docker rm -f "$CTR" >/dev/null 2>&1 || true
  docker volume rm grave-e2e-docker >/dev/null 2>&1 || true
  exit "$rc"
}
trap cleanup EXIT

docker build -q -t gravedecay-e2e -f tests/e2e/Dockerfile tests/e2e
docker rm -f "$CTR" >/dev/null 2>&1 || true
# nested dockerd cannot run on an overlay-on-overlay /var/lib/docker → volume
docker run -d --name "$CTR" --privileged --cgroupns=host \
  -v /sys/fs/cgroup:/sys/fs/cgroup:rw \
  -v grave-e2e-docker:/var/lib/docker \
  --tmpfs /run --tmpfs /run/lock \
  gravedecay-e2e >/dev/null

echo "=== waiting for systemd ==="
for _ in $(seq 1 30); do
  state=$(docker exec "$CTR" systemctl is-system-running 2>/dev/null || true)
  [[ "$state" == running || "$state" == degraded ]] && break
  sleep 1
done

docker cp -q . "$CTR":/repo
docker exec "$CTR" bash /repo/tests/e2e/bootstrap.sh
docker exec "$CTR" chown -R mole:mole /repo

# docker exec -u sets neither USER nor HOME the way a login does; raise.sh
# reads both (set -u), exactly like a real shell session provides them.
as_mole() { docker exec -u mole -e USER=mole -e HOME=/home/mole -w /repo "$CTR" "$@"; }

echo "=== phase 1: first raise (human-at-keyboard sudo) ==="
as_mole bash -c './raise.sh --profile generic </dev/null'
# the box has a wheel rule, so the scoped grant must exist AND sort after it —
# phase 3 is hollow if the first raise skipped or misnamed its sudoers install
docker exec "$CTR" test -f /etc/sudoers.d/zz-gravedecay

echo "=== phase 2: the human leaves — only the raise-installed scoped grant remains ==="
docker exec "$CTR" rm /etc/sudoers.d/zzz-e2e-bootstrap
if as_mole sudo -n mkdir /e2e-should-not-exist 2>/dev/null; then
  echo "FATAL: out-of-scope sudo is still passwordless — phase 3 would prove nothing"
  exit 1
fi

echo "=== phase 3: headless re-raise — the gravedecay-upgrade.service path (#89) ==="
as_mole bash -c './raise.sh --profile generic </dev/null'

echo "=== phase 4: stampless headless re-raise — the pre-stamp sudoers fallback (#96) ==="
as_mole rm /srv/dev/config/.sudoers.stamp
as_mole bash -c './raise.sh --profile generic </dev/null' | tee /tmp/grave-e2e-phase4.log
grep -q "not refreshable without a terminal" /tmp/grave-e2e-phase4.log

echo "=== phase 5: doctor is the contract ==="
as_mole grave doctor
docker exec "$CTR" curl -sf http://127.0.0.1:4712/healthz >/dev/null
docker exec "$CTR" curl -sf -o /dev/null http://127.0.0.1:4711/
docker exec "$CTR" curl -sf -o /dev/null http://127.0.0.1:4713/
echo "=== appliance smoke: PASS ==="
