#!/usr/bin/env bash
# Read-only runtime contract, also installed as scripts/leap42-check.
set -euo pipefail
runtime_user=$(systemctl show -p User t3code 2>/dev/null | sed -n 's/^User=//p' || true)
runtime_home=$(getent passwd "${runtime_user:-$(id -un)}" | cut -d: -f6)
export PATH="$runtime_home/.local/bin:/usr/local/bin:/usr/bin:/bin"
python3 -c 'import sys, PIL, cryptography; assert sys.version_info >= (3, 8)'
node -e 'if (Number(process.versions.node.split(".")[0]) < 22) process.exit(1)'
# Exercise the actual shim and kernel syscall, not just the file's presence.
python3 - <<'PY'
import ctypes, os
lib = ctypes.CDLL('/usr/local/lib/gravedecay-glibc-compat.so', use_errno=True)
lib.memfd_create.argtypes = [ctypes.c_char_p, ctypes.c_uint]
fd = lib.memfd_create(b'grave-doctor', 1)
if fd < 0:
    raise OSError(ctypes.get_errno(), 'memfd_create failed')
os.close(fd)
PY
t3 --version
docker compose version
