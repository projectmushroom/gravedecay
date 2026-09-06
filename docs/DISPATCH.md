# Start work from a Linear issue

On the single-owner Linux appliance, open the Work tab, find an assigned
Linear issue, and tap **Work on this**. Choose a repository and either Codex
or Claude, then **Start work**. The selected CLI must already be installed
and authenticated as the appliance owner. Configure the Linear API key in
settings first. Re-run `raise.sh` after upgrading to install the task runner.
The dashboard receives the owner's HOME and includes `~/.local/bin` in its
service PATH, matching the agent tools used by T3.

The dashboard fetches the issue title, description, and URL from Linear and
starts an interactive agent in a new Git worktree. The source checkout's
committed HEAD is the starting point; uncommitted changes are not copied.
Open the session to handle repository trust, login, or permission prompts.
Dispatch preserves the CLI's configured permissions and does not select a
model or enable permission bypasses. See the [Codex CLI reference](https://developers.openai.com/codex/cli/reference/)
and [Claude CLI reference](https://code.claude.com/docs/en/cli-reference).

The session appears in Work with its issue link, repository, branch, agent,
and process status. "Exited (0)" means the CLI exited successfully, not that
the issue has been completed or reviewed. The pane becomes a shell after the
CLI exits, retaining the worktree and transcript. Killing or resuming the
session follows the normal agent lifecycle; resume opens a shell without
resubmitting the issue to the agent.

Repeated requests for the same Linear issue UUID and repository reuse the
recorded session, including after a browser timeout. They do not start a
second agent, even if a different agent is selected on retry. An existing
session is never overwritten. Use the terminal to continue that work. If its
checkout has been pruned, dispatch reports that recovery or a new named task
is needed rather than opening a shell outside the recorded checkout.

When a PR is opened from the recorded branch against the source repository's
GitHub origin, its link and state appear beside the issue. Lookup uses the
owner's GitHub login, is cached for two minutes, and runs separately from the
main dashboard poll. At most five displayed sessions are looked up per poll.
No PR is created, merged, or deployed automatically by dispatch. The issue
prompt asks the agent to include the issue URL if it opens a PR. Dispatch does
not post comments to Linear or change the issue's workflow state.

## Storage, recovery, and checks

`$GRAVE_ROOT/agents/<name>/task.json` is a private (0600) snapshot containing
`agent` and `issue` (`id`, `title`, `description`, `url`). `meta.json` records
just the issue link/title and selected agent alongside the worktree metadata.
`task-result.json` records process start/exit status. The task runner passes
the issue as a single literal CLI argument; no issue content is typed into a
shell or used as a command. Task snapshots are limited to 64 KiB and descriptions
to 40,000 characters. Oversized issues fail before creating a worktree.

The existing agent-worktree backup includes task snapshots, results, and
metadata along with checkout recovery data. These can contain private issue
text; protect backups accordingly. `grave doctor` checks the runner is present
and saved tasks are private, valid, and consistent with session metadata,
including after pruning a worktree. Removing a CLI does not invalidate an
archived task.

For a manually prepared task using the same format:

```sh
chmod 600 /path/to/task.json
grave agents new issue-attempt-2 --repo my-project --task /path/to/task.json
```

## Authorization and platform scope

Both dispatch and PR lookup use the dashboard's owner identity checks;
mutating requests also use its cross-site request protection. Requests contain
only an issue identifier, a repository name, and an allowlisted agent choice.
The server retrieves the issue itself using the configured Linear key.
Repositories must be primary, non-symlink checkouts under the appliance's
`repos` directory. The operation never invokes sudo.

Dispatch is unavailable on macOS, portable containers, and isolated workspace
backends. Workspace backends cannot invoke the appliance owner's global
`grave.conf`; enabling workspace dispatch requires a separate workspace-aware
launcher. Their existing terminal and T3 flows remain the way to start work.
