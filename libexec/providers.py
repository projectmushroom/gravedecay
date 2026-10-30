#!/usr/bin/env python3
"""The one provider command table for every agent launch on the appliance.

Scheduled jobs, issue dispatch and the Gravekeeper build their CLI argv here, so
the sandbox policy is written once:

* No launch path adds a permission-bypass flag, in any spelling.
* Claude keeps the appliance owner's own permission rules; its CLI has no
  sandbox flag, so a tool the rules refuse fails instead of pausing.
* Codex runs in its own sandbox. Work that is meant to produce changes (jobs,
  dispatch, diggers) may write only inside its working directory; the
  Gravekeeper, which reads the box and calls its MCP tools, is read-only.
  Headless launches never wait for an approval prompt.
* A digger carries its own deny-by-default policy, compiled here from its
  definition into each provider's native form (docs/DIGGERS.md).

Consumers load this file from beside themselves in the installed layout
(``$GRAVE_ROOT/scripts``) or from ``libexec`` in the repository.
"""
import json
import re

PROVIDERS = ("claude", "codex")
# Codex sandbox_mode by launch path. A new path adds a row here, nowhere else.
SANDBOX = {"job": "workspace-write", "dispatch": "workspace-write", "keeper": "read-only",
           "digger": "workspace-write"}
# Never emitted by this table; tests and doctor assert it. Substring match, so
# a value that carries a flag (an MCP argument, an instruction) is refused too.
BYPASS_FLAGS = ("--dangerously-skip-permissions", "--dangerously-bypass-approvals-and-sandbox",
                "--allow-dangerously-skip-permissions", "--dangerously-bypass-hook-trust",
                "--full-auto", "--yolo", "--auto", "--approve-for-me", "danger-full-access",
                "bypassPermissions")
# Claude built-in tools a digger definition may name. Anything else is a typo
# or a tool this table has not reviewed, and is refused.
CLAUDE_TOOLS = ("Bash", "Read", "Edit", "Write", "Glob", "Grep", "NotebookEdit", "TodoWrite", "WebFetch", "WebSearch")
# Codex surfaces a digger never gets, whatever its definition says.
CODEX_OFF = ("apps", "plugins", "browser_use", "computer_use", "in_app_browser", "image_generation",
             "hooks", "multi_agent")
CODEX_SHELL = ("shell_tool", "unified_exec")
RULE = re.compile(r"(?P<tool>[A-Za-z][A-Za-z0-9_-]*(?:__\*)?)(?:\((?P<spec>[^()\x00-\x1f]{1,200})\))?")
MCP_TOOL = re.compile(r"mcp__(?P<server>[a-z][a-z0-9-]{0,31})(?:__(?P<tool>\*|[A-Za-z0-9_-]{1,64}))?")


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


def parse_rule(rule):
    """One permission rule in Claude's syntax: ``(kind, name, spec)`` where kind is
    ``builtin`` or ``mcp``. Raises ValueError for anything this table does not know."""
    match = RULE.fullmatch(rule) if isinstance(rule, str) else None
    if not match:
        raise ValueError("invalid tool rule: %r" % (rule,))
    tool, spec = match["tool"], match["spec"]
    if tool.startswith("mcp__"):
        mcp = MCP_TOOL.fullmatch(tool)
        if not mcp or spec is not None:
            raise ValueError("invalid MCP tool rule: " + rule)
        return "mcp", mcp["server"], mcp["tool"] or "*"
    if tool not in CLAUDE_TOOLS:
        raise ValueError("unknown tool in rule: " + rule)
    return "builtin", tool, spec


def codex_prefix(spec):
    """``git push*`` / ``git push:*`` / ``git push`` as a Codex execpolicy prefix."""
    words = spec.removesuffix("*").removesuffix(":").split()
    if not words or any(c in word for word in words for c in "*?[]"):
        raise ValueError("Codex can refuse only literal command prefixes: Bash(%s)" % spec)
    return words


def digger_policy(definition):
    """The effective tool policy: what is allowed after deny removes it. Raises
    ValueError for an empty allowlist rather than compiling a text-only run."""
    tools = definition["tools"]
    allow = [rule for rule in tools["allow"] if rule not in tools["deny"]]
    servers = dict(tools["mcp"])
    builtins, shell_denies, mcp_allow, mcp_deny = [], [], {}, {}
    for rule in tools["deny"]:
        kind, name, spec = parse_rule(rule)
        if kind == "mcp":
            if name not in servers:
                raise ValueError("deny rule names an MCP server the definition does not define: " + rule)
            if spec == "*":
                servers.pop(name)
            else:
                mcp_deny.setdefault(name, []).append(spec)
        elif name == "Bash" and spec:
            shell_denies.append(codex_prefix(spec) if definition["provider"] == "codex" else spec)
    for rule in allow:
        kind, name, spec = parse_rule(rule)
        if kind == "mcp":
            if name not in tools["mcp"]:
                raise ValueError("allow rule names an MCP server the definition does not define: " + rule)
            if name in servers:
                mcp_allow.setdefault(name, set()).add(spec)
        elif name not in builtins:
            builtins.append(name)
    allow = [rule for rule in allow if parse_rule(rule)[0] == "builtin" or parse_rule(rule)[1] in servers]
    if not allow:
        raise ValueError("empty effective tool allowlist: a digger with no tools refuses to run")
    return dict(allow=allow, builtins=builtins, servers={k: v for k, v in servers.items() if k in mcp_allow},
                mcp_allow=mcp_allow, mcp_deny=mcp_deny, shell_denies=shell_denies)


def digger_files(definition):
    """The generated policy files for one digger, by file name. Claude reads its
    settings and MCP servers from them; Codex reads its refused command prefixes
    from ``digger.rules`` in the digger's own ``CODEX_HOME/rules``."""
    policy = digger_policy(definition)
    tools = definition["tools"]
    if definition["provider"] == "claude":
        settings = {"permissions": {"defaultMode": "dontAsk", "allow": policy["allow"], "deny": list(tools["deny"]),
                                    "disableBypassPermissionsMode": "disable"},
                    "disableAllHooks": True, "enableAllProjectMcpServers": False}
        servers = {name: {"type": "stdio", "command": server["command"], "args": list(server["args"])}
                   for name, server in sorted(policy["servers"].items())}
        return {"claude-settings.json": json.dumps(settings, indent=2, sort_keys=True) + "\n",
                "claude-mcp.json": json.dumps({"mcpServers": servers}, indent=2, sort_keys=True) + "\n"}
    rules = "".join("prefix_rule(pattern=%s, decision=\"forbidden\")\n" % json.dumps(words)
                    for words in policy["shell_denies"])
    return {"digger.rules": "# generated by gravedecay from digger.json; edits are overwritten\n" + rules}


def digger_command(definition, role="digger", prompt_via_stdin=True, layout=None):
    """One digger turn, deny by default, as ``(argv, env)``. ``layout`` names the
    generated policy directory, the instructions file and, for a ``dedicated``
    digger, its HOME and tool PATH; ``env`` is what the runner adds to a scrubbed
    environment. The prompt always arrives on stdin, never as an argument."""
    if not prompt_via_stdin:
        raise ValueError("digger prompts are passed on stdin only")
    provider, identity, layout = definition["provider"], definition["identity"], layout or {}
    if provider not in PROVIDERS:
        raise ValueError("unknown provider: " + str(provider))
    policy, model = digger_policy(definition), definition.get("model") or ""
    env = {}
    if identity == "separate-login":
        env["CODEX_HOME" if provider == "codex" else "CLAUDE_CONFIG_DIR"] = layout["provider_home"]
    elif identity == "dedicated":
        env.update(HOME=layout["home"], PATH=layout["path"])
        if provider == "claude":
            env["DISABLE_AUTOUPDATER"] = "1"  # the tool copy is root-owned and read-only
    if provider == "claude":
        argv = ["claude", "-p", "--output-format", "stream-json", "--verbose",
                "--max-turns", str(definition["budget"]["max_turns"]), "--setting-sources", "",
                "--settings", layout["policy"] + "/claude-settings.json",
                "--strict-mcp-config", "--mcp-config", layout["policy"] + "/claude-mcp.json",
                "--tools", ",".join(policy["builtins"])]
        if definition["tools"]["deny"]:
            argv += ["--disallowedTools", *definition["tools"]["deny"]]
        argv += ["--append-system-prompt-file", layout["instructions"]]
        return argv + (["--model", model] if model else []), env
    if policy["builtins"] and policy["shell_denies"] and identity == "shared":
        raise ValueError("Codex refuses shell commands only from the digger's own CODEX_HOME; "
                         "use identity separate-login or dedicated for Bash deny rules")
    off = CODEX_OFF + (() if policy["builtins"] else CODEX_SHELL)
    argv = ["codex", "exec", "--json", "--ignore-user-config", *sandbox(role), "-c", 'approval_policy="never"',
            "-c", 'web_search="disabled"', *[word for feature in off for word in ("--disable", feature)]]
    for name, server in sorted(policy["servers"].items()):
        key = "mcp_servers." + name  # a bare TOML key: the id pattern admits only [a-z0-9-]
        argv += ["-c", f"{key}.command={json.dumps(server['command'])}",
                 "-c", f"{key}.args={json.dumps(list(server['args']))}",
                 "-c", f'{key}.default_tools_approval_mode="approve"']
        named = sorted(policy["mcp_allow"][name] - {"*"})
        if "*" not in policy["mcp_allow"][name]:
            argv += ["-c", f"{key}.enabled_tools={json.dumps(named)}"]
        if policy["mcp_deny"].get(name):
            argv += ["-c", f"{key}.disabled_tools={json.dumps(sorted(policy['mcp_deny'][name]))}"]
    argv += ["-c", "developer_instructions=" + json.dumps(layout["instructions_text"])]
    return argv + (["-m", model] if model else []) + ["-"], env


def policy_ok(argv):
    """True when a command line carries none of the bypass flags this table refuses."""
    return not any(flag in word for word in argv for flag in BYPASS_FLAGS)
