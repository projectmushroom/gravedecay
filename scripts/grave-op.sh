#!/bin/sh
# Run one durable operation on a grave over the tailnet and exit with its status.
#   scripts/grave-op.sh <box>.<tailnet>.ts.net doctor|digger-selftest|...
# For agents verifying a change without a terminal: the caller's tailnet identity
# must be the grave's owner (docs/API.md). Prints the operation's output.
set -eu
host=${1:?usage: grave-op.sh <host> <action>}; action=${2:?usage: grave-op.sh <host> <action>}
api="https://$host/grave/api/v1"
id="$(date +%s)-$(od -An -N16 -tx1 /dev/urandom | tr -d ' \n')"
curl -sfk -m 30 -X POST -H 'X-Grave-Client: 1' -H 'Content-Type: application/json' \
  -d "{\"id\":\"$id\",\"action\":\"$action\"}" "$api/operations" >/dev/null
state=running
while [ "$state" = running ] || [ "$state" = queued ]; do
  sleep 5
  state=$(curl -sfk -m 30 -H 'X-Grave-Client: 1' "$api/operations?id=$id" | python3 -c 'import json,sys; print(json.load(sys.stdin)["state"])')
done
curl -sfk -m 30 -H 'X-Grave-Client: 1' "$api/operations?id=$id" | python3 -c '
import json, re, sys
d = json.load(sys.stdin)
print(re.sub(r"\x1b\[[0-9;]*m", "", "".join(e["text"] for e in d["events"])))
print("operation", d["state"], "exit", d["exit_code"])
sys.exit(0 if d["state"] == "succeeded" else 1)'
