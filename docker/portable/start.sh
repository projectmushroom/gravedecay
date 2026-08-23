#!/usr/bin/env bash
# The work plane intentionally has no systemd or Docker socket. One small
# shell supervisor is enough for its three user-owned processes.
set -euo pipefail

mkdir -p "$GRAVE_ROOT/repos" "$T3_BASE_DIR" "$GRAVE_ROOT/config" "$GRAVE_ROOT/logs"

t3 serve --mode web --host 0.0.0.0 --port 4711 --base-dir "$T3_BASE_DIR" "$GRAVE_ROOT/repos" &
t3_pid=$!
python3 /opt/gravedecay/gravedecay.py &
dashboard_pid=$!
ttyd -p 4713 -i 0.0.0.0 -W --url-arg --base-path /term \
  -I /opt/gravedecay/web/term/index.html /opt/gravedecay/webterm &
term_pid=$!

stop() { kill "$t3_pid" "$dashboard_pid" "$term_pid" 2>/dev/null || true; }
trap stop EXIT INT TERM
wait -n "$t3_pid" "$dashboard_pid" "$term_pid"
