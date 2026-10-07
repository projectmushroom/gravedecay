---
name: Bug
about: Something on the box did not behave as documented
labels: bug
---

**What happened**

**What you expected**

**How to reproduce**

```sh
# the command you ran
```

**Box**

- OS and version (`cat /etc/os-release`):
- Host profile (`generic`, `aws`, `t2-macbook`, `steam-machine`):
- gravedecay version (`grave upgrade` prints it; or `git -C $GRAVE_ROOT/repos/gravedecay log -1 --oneline`):
- `grave doctor` output, failing lines only:

**Logs**

`grave logs <unit>` or `journalctl -u gravedecay-agents` around the time of the failure. Strip tokens and hostnames you do not want public.
