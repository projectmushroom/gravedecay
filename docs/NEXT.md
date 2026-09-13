# Direction and next features

## What gravedecay is building

gravedecay turns a machine into a persistent development environment that can
be operated from any of the owner's devices. Repositories, agent work, and
backing services stay on the appliance; T3, the dashboard, terminal, and native
clients provide access. Native processes and real Unix identities let agents
use normal development tools, while Tailscale keeps the access boundary small.

The latest implementation already covers provisioning, upgrades, gaming mode,
notifications, isolated agent worktrees, issue-driven jobs, and several client
surfaces. The durable product promise is that work continues when the client
leaves, can be understood when the owner returns, and can be recovered when a
machine fails. Doctor turns that promise into inspectable checks.

The next phase should make unattended work dependable before adding more ways
to launch it. This is an engineering prioritization based on the implementation
and open backlog, rather than evidence of user demand.

## First delivery: recovery you can check

The backup implementation verified artifacts when writing them but pruned old
copies before checking whether the overall run succeeded. A failure could
therefore remove a good recovery point. It also had no stored checksums to test
whether a copied or aging backup was still intact.

The recovery change adds two connected capabilities:

- Private staging, serialized runs, completed-directory publication, and
  retention only after success keep failed attempts out of the recovery set.
- A checksum inventory, `grave backup verify`, restore preflight, and a doctor
  inventory check make storage damage visible before a restore mutates data.

See [RECOVERY.md](RECOVERY.md) for guarantees and limits. This is groundwork for
[off-box recovery (#81)](https://github.com/projectmushroom/gravedecay/issues/81),
not its completion: local backups still disappear with the disk.

## Next priorities

| Order | Outcome | Smallest useful delivery | Acceptance condition |
|---|---|---|---|
| 1 | Recover after disk loss | Encrypted remote replication and a documented fresh-machine restore, building on #81 | Restore a copied project and dirty agent work on a disposable replacement host; doctor detects stale replication |
| 2 | Bound unattended spending | Persist usage and configurable prices (#86), then opt-in spend alerts (#107) | Threshold notifications deduplicate across restarts; estimates and unknown pricing stay visible |
| 3 | Explain what the appliance changed | A common privileged-action audit record (#115) | CLI and dashboard actions record actor, action, outcome, and time without credentials; doctor checks access permissions |
| 4 | Make first use verifiable | A setup checklist from actual pairing, agent, notification, and backup state (#116) | A new owner can reach a successful first agent task and a verified recovery point without reading service logs |
| 5 | Protect reused hardware | Battery/disk health reporting (#113), then opt-in thermal policy (#114) | Unsupported sensors degrade clearly; automatic pauses preserve remote access and never silently resume a manual pause |

The open identity and security reports (#47, #141–#147) need triage against the
current implementation before expanding collaborator access. They are backlog
reports, not findings validated by this review. Recovery privacy work here does
not claim to resolve owner-state isolation across the rest of the platform.

Fleet routing (#105) and additional agent adapters (#111) can follow these
foundations. Each adds more state to operate and recover, so the delivery gate
should remain a documented behavior, an executable test, and a doctor invariant.
