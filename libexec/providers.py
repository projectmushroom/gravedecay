#!/usr/bin/env python3
"""The one provider command table for every agent launch on the appliance.

Scheduled jobs, issue dispatch and the Gravekeeper build their CLI argv here, so
the sandbox policy is written once:

* No launch path adds a permission-bypass flag, in any spelling.
* Claude keeps the appliance owner's own permission rules; its CLI has no
  sandbox flag, so a tool the rules refuse fails instead of pausing.
* Codex runs in its own sandbox. Work that is meant to produce changes (jobs,
  dispatch) may write only inside its working directory; the Gravekeeper, which
  reads the box and calls its MCP tools, is read-only. Headless launches never
  wait for an approval prompt.

Consumers load this file from beside themselves in the installed layout
(``$GRAVE_ROOT/scripts``) or from ``libexec`` in the repository.
"""
import json

PROVIDERS = ("claude", "codex")
# Codex sandbox_mode by launch path. A new path adds a row here, nowhere else.
SANDBOX = {"job": "workspace-write", "dispatch": "workspace-write", "keeper": "read-only"}
# Never emitted by this table; tests and doctor assert it.
BYPASS_FLAGS = ("--dangerously-skip-permissions", "--dangerously-bypass-approvals-and-sandbox",
                "--full-auto", "--yolo", "danger-full-access")


def sandbox(role):
    """Codex sandbox for one launch path, as a config override: the only form
    every Codex subcommand accepts, ``exec resume`` included."""
    return ["-c", 'sandbox_mode="%s"' % SANDBOX[role]]


def job_command(provider):
    """Headless scheduled run: prompt on stdin, plain output, no approval prompts."""
    if provider not in PROVIDERS:
        raise ValueError("unknown provider: " + str(provider))
    if provider == "codex":
        return ["codex", "exec", *sandbox("job"), "-c", 'approval_policy="never"', "--color", "never", "-"]
    return ["claude", "-p"]


def dispatch_command(provider, prompt):
    """Interactive issue session in a tmux pane; the owner answers prompts there.
    The prompt is one literal argument, never shell text."""
    if provider not in PROVIDERS:
        raise ValueError("unknown provider: " + str(provider))
    if provider == "codex":
        return ["codex", *sandbox("dispatch"), prompt]
    return ["claude", prompt]


def keeper_command(provider, mcp, work_dir, model="", session=None):
    """One Gravekeeper turn: the owner's CLI configuration plus the Keeper MCP server."""
    if provider not in PROVIDERS:
        raise ValueError("unknown provider: " + str(provider))
    if provider == "claude":
        config = json.dumps({"mcpServers": {"keeper": {"command": mcp[0], "args": mcp[1:]}}})
        argv = ["claude", "-p", "--output-format", "stream-json", "--verbose", "--max-turns", "30",
                "--mcp-config", config, "--allowedTools", "mcp__keeper"]
        return argv + (["--model", model] if model else []) + (["--resume", session] if session else [])
    argv = ["codex", "exec"] + (["resume", session] if session else ["-C", work_dir])
    argv += ["--json", "--skip-git-repo-check", "-c", 'approval_policy="never"', *sandbox("keeper"),
             "-c", f"mcp_servers.keeper.command={json.dumps(mcp[0])}", "-c", f"mcp_servers.keeper.args={json.dumps(mcp[1:])}",
             "-c", 'mcp_servers.keeper.default_tools_approval_mode="approve"']
    return argv + (["-m", model] if model else []) + ["-"]


def policy_ok(argv):
    """True when a command line carries none of the bypass flags this table refuses."""
    return not any(flag in word for word in argv for flag in BYPASS_FLAGS)
