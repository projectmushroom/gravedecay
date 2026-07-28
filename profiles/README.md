# Host profiles

Machine-specific quirks, applied once by `raise.sh --profile <name>`. A profile
is a bash file defining `profile_apply()`. Rules:

- Idempotent — applying twice changes nothing.
- Every invariant a profile establishes gets a matching `CHECK_*=1` flag in
  `/etc/gravedecay/grave.conf` so `grave doctor` enforces it forever. Use the
  `conf_set` helper from `profiles/lib.sh`.
- Comment the *why* (the crash, the errata link), not just the what.

Template:

```bash
# profiles/myhost.sh — <one line: what hardware, what quirk>
# shellcheck disable=SC1091
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

profile_apply() {
  # ... drop-ins, masks, services ...
  conf_set CHECK_SLEEP_MASKED 1
}
```
