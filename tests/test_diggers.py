"""Diggers: definitions, identity tiers, logins, policy, the runner, Slack intake and doctor."""
import base64
import hashlib
import http.server
import importlib.util
import json
import os
import pathlib
import re
import socket
import stat
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import unittest.mock
import uuid

ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "libexec/diggers.py"
GRAVE = (ROOT / "bin/grave").read_text()
RAISE = (ROOT / "raise.sh").read_text()


def load(name, env):
    """diggers.py reads GRAVE_ROOT and the test guard at import time."""
    saved = {key: os.environ.get(key) for key in env}
    os.environ.update(env)
    try:
        spec = importlib.util.spec_from_file_location(name, SCRIPT)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


providers = load("diggers_schema", {}).table


def definition(**overrides):
    value = {"name": "whitelabel", "provider": "codex", "model": "", "identity": "dedicated",
             "repo": "app", "work": "clone",
             "tools": {"mcp": {"whitelabel": {"command": "node", "args": [".grave/mcp/whitelabel.js"]}},
                       "allow": ["mcp__whitelabel__*"], "deny": ["Bash(git push*)"]},
             "budget": {"max_turns": 30, "timeout_s": 1800, "daily_turns": 200, "daily_usd": 10.0},
             "pause_in_gaming": True, "enabled": True}
    value.update(overrides)
    return value


FAKE_CODEX = """#!/bin/sh
home="${CODEX_HOME:-$HOME/.codex}"
case "$1 $2" in
  "login status") [ -f "$home/auth.json" ] && { echo "Logged in using ChatGPT"; exit 0; }; echo "Not logged in"; exit 1 ;;
  "login --device-auth") mkdir -p "$home"; echo '{"tokens":"SECRET-TOKEN"}' > "$home/auth.json"; echo "enter code WXYZ-1234" ;;
  "login --with-api-key") mkdir -p "$home"; read key; echo "{\\"key\\":\\"$key\\"}" > "$home/auth.json" ;;
esac
"""
FAKE_CLAUDE = """#!/bin/sh
home="${CLAUDE_CONFIG_DIR:-$HOME/.claude}"
case "$1 $2" in
  "auth status") if [ -f "$home/.credentials.json" ]; then echo '{"loggedIn": true, "email": "x@example.test"}'; else echo '{"loggedIn": false}'; exit 1; fi ;;
  "auth login") mkdir -p "$home"; echo '{"token":"SECRET-TOKEN"}' > "$home/.credentials.json"; echo "open https://claude.ai/oauth $3" ;;
esac
"""


class SchemaTests(unittest.TestCase):
    def setUp(self):
        self.module = load("diggers_validate", {"GRAVE_DIGGER_TEST": "1"})

    def assertRefused(self, value, fragment, instructions=""):
        with self.assertRaises(self.module.DiggerError) as caught:
            self.module.validate(value, instructions=instructions)
        self.assertIn(fragment, str(caught.exception))

    def test_the_epic_example_is_valid(self):
        self.assertEqual(self.module.validate(definition(), "whitelabel")["identity"], "dedicated")

    def test_names_follow_the_job_rule_and_keeper_is_reserved(self):
        for good in ("wl", "white-label", "a" + "b" * 19):
            self.module.check_name(good)
        for bad in ("w", "Wl", "1wl", "wl_x", "a" * 21, "-wl", "wl/../x", "keeper"):
            with self.assertRaises(self.module.DiggerError, msg=bad):
                self.module.check_name(bad)
        self.assertRefused(definition(name="keeper"), "reserved")

    def test_unknown_and_missing_keys_are_rejected(self):
        self.assertRefused(dict(definition(), sandbox="danger-full-access"), "unknown key(s): sandbox")
        value = definition(); del value["budget"]
        self.assertRefused(value, "missing key(s): budget")
        tools = dict(definition()["tools"], extra=[])
        self.assertRefused(definition(tools=tools), "exactly mcp, allow and deny")
        mcp = {"whitelabel": {"command": "node", "args": [], "env": {"TOKEN": "x"}}}
        self.assertRefused(definition(tools=dict(definition()["tools"], mcp=mcp)), "exactly command and args")
        self.assertRefused(definition(budget=dict(definition()["budget"], daily_tokens=1)), "budget must hold exactly")

    def test_identity_work_provider_and_types(self):
        self.assertRefused(definition(work="worktree"), "needs work='clone'")
        self.assertRefused(definition(identity="shared", work="clone"), "needs work='worktree'")
        self.assertRefused(definition(identity="root"), "identity must be one of")
        self.assertRefused(definition(provider="opencode"), "provider must be one of")
        self.assertRefused(definition(enabled="yes"), "enabled must be true or false")
        self.assertRefused(definition(budget=dict(definition()["budget"], max_turns=True)), "budget.max_turns")
        self.assertRefused(definition(budget=dict(definition()["budget"], daily_usd=float("inf"))), "daily_usd")
        self.assertRefused(definition(model="gpt; rm -rf"), "model must be")
        self.assertRefused(definition(repo="../etc"), "repo must name")

    def test_tool_rules_are_known_and_reference_defined_servers(self):
        tools = definition()["tools"]
        self.assertRefused(definition(tools=dict(tools, allow=["Shell"])), "unknown tool in rule: Shell")
        self.assertRefused(definition(tools=dict(tools, allow=["mcp__other__*"])), "does not define")
        self.assertRefused(definition(tools=dict(tools, allow=["Read", "Read"])), "distinct")
        self.assertRefused(definition(tools=dict(tools, mcp={"Bad_Id": tools["mcp"]["whitelabel"]})), "server id")

    def test_an_empty_effective_allowlist_refuses_rather_than_running_text_only(self):
        tools = definition()["tools"]
        self.assertRefused(definition(tools=dict(tools, allow=[])), "empty effective tool allowlist")
        # deny removes everything that allow granted
        self.assertRefused(definition(tools=dict(tools, deny=["mcp__whitelabel__*"])), "empty effective tool allowlist")
        self.assertRefused(definition(tools=dict(tools, allow=["Read"], deny=["Read"])), "empty effective tool allowlist")

    def test_a_bypass_flag_anywhere_in_the_compiled_command_is_refused(self):
        tools = definition()["tools"]
        mcp = {"whitelabel": {"command": "node", "args": ["tool.js", "--dangerously-skip-permissions"]}}
        self.assertRefused(definition(tools=dict(tools, mcp=mcp)), "permission-bypass flag")
        self.assertRefused(definition(), "permission-bypass flag", instructions="Run codex --yolo when stuck.")


class CompileTests(unittest.TestCase):
    def setUp(self):
        self.module = load("diggers_compile", {"GRAVE_DIGGER_TEST": "1", "GRAVE_ROOT": "/srv/dev"})

    def test_codex_dedicated_compiles_to_the_exact_deny_by_default_argv(self):
        value = definition(tools={"mcp": {"whitelabel": {"command": "node", "args": [".grave/mcp/whitelabel.js"]}},
                                  "allow": ["mcp__whitelabel__create"], "deny": ["mcp__whitelabel__delete"]})
        argv, env = self.module.compile_command(value, "Make white labels.")
        self.assertEqual(argv, [
            "codex", "exec", "--json", "--ignore-user-config", "-c", 'sandbox_mode="workspace-write"',
            "-c", 'approval_policy="never"', "-c", 'web_search="disabled"',
            "--disable", "apps", "--disable", "plugins", "--disable", "browser_use", "--disable", "computer_use",
            "--disable", "in_app_browser", "--disable", "image_generation", "--disable", "hooks",
            "--disable", "multi_agent", "--disable", "shell_tool", "--disable", "unified_exec",
            "-c", 'mcp_servers.whitelabel.command="node"', "-c", 'mcp_servers.whitelabel.args=[".grave/mcp/whitelabel.js"]',
            "-c", 'mcp_servers.whitelabel.default_tools_approval_mode="approve"',
            "-c", 'mcp_servers.whitelabel.enabled_tools=["create"]',
            "-c", 'mcp_servers.whitelabel.disabled_tools=["delete"]',
            "-c", 'developer_instructions="Make white labels."', "-"])
        self.assertEqual(env, {"HOME": "/srv/dev/diggers/whitelabel",
                               "PATH": "/srv/dev/diggers/.tools/bin:/usr/local/bin:/usr/bin:/bin"})
        self.assertTrue(providers.policy_ok(argv))

    def test_claude_separate_login_compiles_settings_strict_mcp_and_its_own_config_dir(self):
        value = definition(provider="claude", identity="separate-login", work="worktree", model="sonnet",
                           tools={"mcp": {"whitelabel": {"command": "node", "args": ["wl.js"]}},
                                  "allow": ["mcp__whitelabel__*", "Read", "Bash(git status)"],
                                  "deny": ["Bash(git push*)"]})
        argv, env = self.module.compile_command(value, "ignored for Claude: read from the file")
        base = "/srv/dev/config/diggers/whitelabel"
        self.assertEqual(argv, [
            "claude", "-p", "--output-format", "stream-json", "--verbose", "--max-turns", "30",
            "--setting-sources", "", "--settings", f"{base}/policy/claude-settings.json",
            "--strict-mcp-config", "--mcp-config", f"{base}/policy/claude-mcp.json",
            "--tools", "Read,Bash", "--disallowedTools", "Bash(git push*)",
            "--append-system-prompt-file", f"{base}/DIGGER.md", "--model", "sonnet"])
        self.assertEqual(env, {"CLAUDE_CONFIG_DIR": f"{base}/claude"})
        files = providers.digger_files(value)
        settings = json.loads(files["claude-settings.json"])
        self.assertEqual(settings["permissions"], {"defaultMode": "dontAsk", "allow": value["tools"]["allow"],
                                                   "deny": ["Bash(git push*)"], "disableBypassPermissionsMode": "disable"})
        self.assertTrue(settings["disableAllHooks"])
        self.assertEqual(json.loads(files["claude-mcp.json"]),
                         {"mcpServers": {"whitelabel": {"type": "stdio", "command": "node", "args": ["wl.js"]}}})
        for text in files.values():
            self.assertTrue(providers.policy_ok([text]))

    def test_provider_homes_appear_in_env_only_for_separate_login(self):
        for identity, work in (("shared", "worktree"), ("separate-login", "worktree"), ("dedicated", "clone")):
            for provider in ("codex", "claude"):
                value = definition(provider=provider, identity=identity, work=work,
                                   tools=dict(definition()["tools"], deny=[]))
                _, env = self.module.compile_command(value, "x")
                homes = {"CODEX_HOME", "CLAUDE_CONFIG_DIR"} & set(env)
                if identity == "separate-login":
                    self.assertEqual(homes, {"CODEX_HOME" if provider == "codex" else "CLAUDE_CONFIG_DIR"})
                    self.assertTrue(env.popitem()[1].startswith("/srv/dev/config/diggers/"))
                else:
                    self.assertEqual(homes, set(), (identity, provider))
                if identity == "shared":
                    self.assertEqual(env, {})

    def test_codex_shell_is_off_unless_a_builtin_is_allowed_and_denies_compile_to_rules(self):
        tools = {"mcp": {}, "allow": ["Bash(npm test)"], "deny": ["Bash(git push*)", "Bash(rm -rf:*)"]}
        value = definition(identity="separate-login", work="worktree", tools=tools)
        argv, _ = self.module.compile_command(value, "x")
        self.assertNotIn("shell_tool", argv)
        self.assertEqual(self.module.codex_rules(value).splitlines()[1:], [
            'prefix_rule(pattern=["git", "push"], decision="forbidden")',
            'prefix_rule(pattern=["rm", "-rf"], decision="forbidden")'])
        with self.assertRaises(self.module.DiggerError) as caught:
            self.module.compile_command(dict(value, identity="shared"), "x")
        self.assertIn("own CODEX_HOME", str(caught.exception))
        with self.assertRaises(self.module.DiggerError):
            self.module.compile_command(dict(value, tools=dict(tools, deny=["Bash(git *push)"])), "x")
        self.assertIsNone(self.module.codex_rules(dict(value, identity="shared")))

    def test_digger_prompts_never_travel_as_arguments(self):
        with self.assertRaises(ValueError):
            providers.digger_command(definition(), "digger", False, {})


class LifecycleTests(unittest.TestCase):
    """The CLI in test mode: every identity is the current user; no sudo, useradd or runuser."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = pathlib.Path(self.tmp.name)
        self.root = base / "root"
        (self.root / "config").mkdir(parents=True)
        self.root.chmod(0o755)
        repo = self.root / "repos/app"
        repo.mkdir(parents=True)
        git = ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.test"]
        subprocess.run(["git", "init", "-q", "-b", "trunk", str(repo)], check=True)
        (repo / "README").write_text("app\n")
        subprocess.run([*git, "add", "README"], check=True)
        subprocess.run([*git, "commit", "-q", "-m", "init"], check=True)
        (repo / "dirty").write_text("uncommitted\n")
        subprocess.run([*git, "remote", "add", "origin", "https://github.com/example/app.git"], check=True)
        self.bin = base / "bin"
        self.bin.mkdir()
        for name, text in (("codex", FAKE_CODEX), ("claude", FAKE_CLAUDE)):
            (self.bin / name).write_text(text)
            (self.bin / name).chmod(0o755)
        self.owner = base / "owner"
        self.owner.mkdir(mode=0o700)
        self.env = dict(os.environ, GRAVE_ROOT=str(self.root), GRAVE_DIGGER_TEST="1", HOME=str(self.owner),
                        PATH=f"{self.bin}:{os.environ['PATH']}")
        self.env.pop("CODEX_HOME", None); self.env.pop("CLAUDE_CONFIG_DIR", None)

    def tearDown(self):
        self.tmp.cleanup()

    def grave(self, *args, ok=True, input=None):
        proc = subprocess.run([sys.executable, str(SCRIPT), *args], env=self.env, capture_output=True, text=True,
                              input=input, cwd=self.tmp.name)
        if ok:
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        return proc

    def doctor(self):
        lines = [line.split("\t", 1) for line in self.grave("check").stdout.splitlines()]
        return {(level, message) for level, message in lines}

    def mode(self, path):
        return stat.S_IMODE(os.lstat(path).st_mode)

    def test_dedicated_codex_digger_gets_home_clone_policy_and_its_own_login(self):
        self.grave("new", "wl", "--repo", "app", "--provider", "codex", "--identity", "dedicated",
                   "--mcp", "whitelabel=node .grave/mcp/whitelabel.js", "--deny", "Bash(git push*)")
        store = self.root / "config/diggers/wl"
        self.assertEqual((self.mode(self.root / "config/diggers"), self.mode(store)), (0o700, 0o700))
        self.assertEqual((self.mode(store / "digger.json"), self.mode(store / "DIGGER.md")), (0o600, 0o600))
        saved = json.loads((store / "digger.json").read_text())
        self.assertEqual(saved["tools"]["allow"], ["mcp__whitelabel__*"])
        self.assertEqual(saved["work"], "clone")
        home = self.root / "diggers/wl"
        self.assertEqual(self.mode(home), 0o700)
        clone = home / "repos/app"
        out = lambda *a: subprocess.run(["git", "-C", str(clone), *a], capture_output=True, text=True).stdout.strip()
        self.assertEqual(out("remote", "get-url", "origin"), "https://github.com/example/app.git")
        self.assertEqual(out("branch", "--show-current"), "trunk")
        self.assertEqual(out("status", "--porcelain"), "")   # committed HEAD only: the owner's dirty file stays home
        self.assertFalse((clone / "dirty").exists())
        self.assertEqual(list(home.glob("repos/.*")), [])     # no partial clone or bundle left behind
        self.assertFalse((store / ".clone.bundle").exists())
        policy = self.root / "diggers/.policy/wl"
        self.assertEqual(self.mode(policy), 0o750)
        self.assertEqual(sorted(p.name for p in policy.iterdir()), ["DIGGER.md", "digger.json"])
        self.assertEqual({self.mode(p) for p in policy.iterdir()}, {0o640})
        self.assertEqual((home / ".codex/rules/digger.rules").read_text().splitlines()[1],
                         'prefix_rule(pattern=["git", "push"], decision="forbidden")')
        self.assertTrue(os.access(self.root / "diggers/.tools/bin/codex", os.X_OK))
        findings = self.doctor()
        self.assertIn(("warn", "digger wl: codex not logged in — run: grave digger login wl"), findings)
        self.assertNotIn("fail", {level for level, _ in findings}, findings)

        login = self.grave("login", "wl")
        self.assertIn("enter code WXYZ-1234", login.stdout)
        self.assertTrue((home / ".codex/auth.json").exists())
        self.assertFalse((self.owner / ".codex").exists())    # the owner's login is untouched
        status = json.loads(self.grave("status", "wl").stdout)
        self.assertEqual(status, {"name": "wl", "provider": "codex", "identity": "dedicated", "enabled": True,
                                  "user": "grave-digger-wl", "loggedIn": True})
        self.assertNotIn("SECRET-TOKEN", self.grave("status").stdout + self.grave("check").stdout)
        self.assertIn(("ok", "digger wl: codex logged in as " + __import__("pwd").getpwuid(os.geteuid()).pw_name),
                      self.doctor())

    def test_separate_login_claude_digger_keeps_its_login_out_of_the_owner_config(self):
        self.grave("new", "wl2", "--repo", "app", "--provider", "claude", "--identity", "separate-login")
        home = self.root / "config/diggers/wl2/claude"
        self.assertEqual(self.mode(home), 0o700)
        policy = self.root / "config/diggers/wl2/policy"
        self.assertEqual(sorted(p.name for p in policy.iterdir()), ["claude-mcp.json", "claude-settings.json"])
        self.assertIn(("warn", "digger wl2: claude not logged in — run: grave digger login wl2"), self.doctor())
        self.assertIn("open https://claude.ai/oauth --claudeai", self.grave("login", "wl2").stdout)
        self.assertIn("--console", self.grave("login", "wl2", "--console").stdout)
        self.assertTrue((home / ".credentials.json").exists())
        self.assertFalse((self.owner / ".claude").exists())
        self.assertTrue(json.loads(self.grave("status", "wl2").stdout)["loggedIn"])
        self.assertIn("use --console", self.grave("login", "wl2", "--api-key", ok=False, input="k\n").stderr)

    def test_codex_api_key_login_reads_the_key_from_stdin_never_argv(self):
        self.grave("new", "wl3", "--repo", "app", "--identity", "separate-login")
        self.grave("login", "wl3", "--api-key", input="sk-test-123\n")
        self.assertIn("sk-test-123", (self.root / "config/diggers/wl3/codex/auth.json").read_text())

    def test_shared_digger_uses_the_owner_login_and_refuses_a_separate_one(self):
        self.grave("new", "sh", "--repo", "app", "--provider", "codex", "--identity", "shared")
        self.assertIn("uses the owner's own codex login", self.grave("login", "sh", ok=False).stderr)
        self.assertIn(("warn", "digger sh: codex not logged in — log in as the appliance owner"), self.doctor())

    def test_names_are_never_reused_and_bad_definitions_fail_doctor(self):
        self.grave("new", "wl", "--repo", "app", "--identity", "separate-login")
        self.assertIn("already exists", self.grave("new", "wl", "--repo", "app", ok=False).stderr)
        self.assertIn("reserved", self.grave("new", "keeper", "--repo", "app", ok=False).stderr)
        self.assertIn("primary, non-symlink", self.grave("new", "zz", "--repo", "nope", ok=False).stderr)
        path = self.root / "config/diggers/wl/digger.json"
        value = json.loads(path.read_text())
        path.write_text(json.dumps(dict(value, sandbox="danger-full-access")))
        self.assertIn(("fail", "digger wl: unknown key(s): sandbox"), self.doctor())
        path.write_text("{not json")
        self.assertTrue(any(level == "fail" and "does not parse" in message for level, message in self.doctor()))
        path.chmod(0o644)
        path.write_text(json.dumps(value))
        self.assertTrue(any(level == "fail" and "mode 600" in message for level, message in self.doctor()))

    def test_editing_a_definition_is_drift_until_enable_recompiles_it(self):
        self.grave("new", "wl", "--repo", "app", "--provider", "claude", "--identity", "separate-login")
        path = self.root / "config/diggers/wl/digger.json"
        value = json.loads(path.read_text())
        value["tools"]["allow"].append("Edit")
        path.write_text(json.dumps(value))
        drift = [m for level, m in self.doctor() if level == "fail"]
        self.assertEqual(drift, ["digger wl: compiled policy drifted (claude-settings.json) — run: grave digger enable wl"])
        self.grave("disable", "wl")
        self.assertFalse(json.loads(path.read_text())["enabled"])
        self.grave("enable", "wl")
        self.assertTrue(json.loads(path.read_text())["enabled"])
        self.assertNotIn("fail", {level for level, _ in self.doctor()})
        self.assertIn('"Edit"', (self.root / "config/diggers/wl/policy/claude-settings.json").read_text())

    def test_remove_keeps_home_clone_and_login_unless_purged(self):
        self.grave("new", "wl", "--repo", "app")
        self.grave("login", "wl")
        self.assertIn("kept", self.grave("remove", "wl").stdout)
        self.assertEqual(self.grave("list").stdout.strip(), "No diggers. Define one: grave digger new <name> --repo <repo>")
        self.assertTrue((self.root / "diggers/wl/repos/app/.git").is_dir())
        self.assertTrue((self.root / "diggers/wl/.codex/auth.json").exists())
        self.assertEqual(len(list((self.root / "config/diggers/.removed").iterdir())), 1)
        self.assertFalse((self.root / "diggers/.policy/wl").exists())
        self.grave("new", "wl", "--repo", "app")    # the retained identity and clone are adopted
        self.grave("remove", "wl", "--purge")
        self.assertFalse((self.root / "diggers/wl").exists())
        self.assertFalse((self.root / "config/diggers/wl").exists())

    def test_a_dedicated_tool_must_be_a_native_build_and_its_copy_drops_set_id_bits(self):
        module = load("diggers_tools", {"GRAVE_DIGGER_TEST": "1", "GRAVE_ROOT": str(self.root)})
        with self.assertRaises(module.DiggerError) as caught:
            module.tool_source("codex", str(self.bin / "codex"), native_only=True)
        self.assertIn("is a script, not a native build", str(caught.exception))
        install = pathlib.Path(self.tmp.name) / "mise/codex/1.0"
        (install / "bin").mkdir(parents=True)
        (install / "codex-resources").mkdir()
        (install / "codex-resources/rg").write_bytes(b"\x7fELFrg")
        binary = install / "bin/codex"
        binary.write_bytes(b"\x7fELF" + b"\0" * 64)
        binary.chmod(0o4755)
        self.assertTrue(module.install_tool("codex", str(binary)))
        link = self.root / "diggers/.tools/bin/codex"
        self.assertTrue(os.readlink(link).startswith("../codex-") and os.readlink(link).endswith("/install/bin/codex"))
        copied = (link.parent / os.readlink(link)).resolve()
        self.assertEqual(self.mode(copied), 0o755)
        self.assertTrue((copied.parent.parent / "codex-resources/rg").exists())
        self.assertFalse(module.install_tool("codex", str(binary)))   # unchanged source: no second copy
        # Found on the box: a purge that followed a link in a digger HOME left the shared copy 0600.
        home = self.root / "diggers/victim"
        (home / ".codex/tmp").mkdir(parents=True)
        (home / ".codex/tmp/codex").symlink_to(copied)
        module.make_writable(home)
        self.assertEqual(self.mode(copied), 0o755)                    # never through a link
        copied.chmod(0o600)
        self.assertTrue(module.install_tool("codex", str(binary)))    # a copy others cannot run is rebuilt
        self.assertEqual(self.mode((link.parent / os.readlink(link)).resolve()), 0o755)


    def test_the_owner_tool_is_found_past_a_launcher_script(self):
        # Omarchy's ~/.local/bin/codex is a bash wrapper around `mise x codex`.
        module = load("diggers_resolve", {"GRAVE_DIGGER_TEST": "", "GRAVE_ROOT": str(self.root)})
        native = pathlib.Path(self.tmp.name) / "native-codex"
        native.write_bytes(b"\x7fELF" + b"\0" * 16)
        native.chmod(0o755)
        env = {"PATH": str(self.bin), "DIGGER_CODEX_BIN": str(native)}
        with unittest.mock.patch.dict(os.environ, env):
            self.assertEqual(module.owner_tool("codex"), str(native))
        with unittest.mock.patch.dict(os.environ, {"PATH": str(self.bin), "DIGGER_CODEX_BIN": ""}):
            self.assertEqual(module.owner_tool("codex"), str(self.bin / "codex"))   # refused later, with the fix named
        # A mise shim on the unit's PATH is a symlink to the mise binary: never the provider.
        shims = pathlib.Path(self.tmp.name) / "shims"
        shims.mkdir()
        mise = pathlib.Path(self.tmp.name) / "mise"
        mise.write_bytes(b"\x7fELF" + b"\0" * 16)
        mise.chmod(0o755)
        (shims / "codex").symlink_to(mise)
        with unittest.mock.patch.dict(os.environ, {"PATH": str(shims), "DIGGER_CODEX_BIN": str(native)}):
            self.assertEqual(module.owner_tool("codex"), str(native))
        with unittest.mock.patch.dict(os.environ, {"PATH": str(shims), "DIGGER_CODEX_BIN": ""}):
            with self.assertRaises(module.DiggerError):
                module.owner_tool("codex")


RUNNER_CODEX = r"""#!/usr/bin/env python3
import json, os, subprocess, sys, time
args = sys.argv[1:]
home = os.environ.get("CODEX_HOME") or os.path.join(os.environ["HOME"], ".codex")
if args[:2] == ["login", "status"]:
    sys.exit(0 if os.path.exists(os.path.join(home, "auth.json")) else 1)
if args[:2] in (["login", "--device-auth"], ["login", "--with-api-key"]):
    os.makedirs(home, exist_ok=True); open(os.path.join(home, "auth.json"), "w").write("{}"); sys.exit(0)
if args[:1] != ["exec"]:
    sys.exit(2)
os.makedirs(home, exist_ok=True)
with open(os.path.join(home, "argv.jsonl"), "a") as log:
    log.write(json.dumps({"argv": args, "cwd": os.getcwd()}) + "\n")
prompt = sys.stdin.read()
emit = lambda value: print(json.dumps(value), flush=True)
emit({"type": "thread.started", "thread_id": args[2] if args[1] == "resume" else "thread-1"})
if "sleep" in prompt:
    time.sleep(30)
config = dict(args[i + 1].split("=", 1) for i, word in enumerate(args) if word == "-c")
servers = sorted({key.split(".")[1] for key in config if key.startswith("mcp_servers.")})
if "acme" in prompt and servers:   # the one tool: as Codex would, start the configured MCP server and call it
    server = servers[0]
    command = [json.loads(config[f"mcp_servers.{server}.command"]), *json.loads(config[f"mcp_servers.{server}.args"])]
    calls = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
             {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
              "params": {"name": "create_whitelabel", "arguments": {"name": "acme", "domain": "acme.example"}}}]
    emit({"type": "item.started", "item": {"type": "mcp_tool_call", "server": server, "tool": "create_whitelabel", "arguments": {"name": "acme"}}})
    reply = subprocess.run(command, input="".join(json.dumps(c) + "\n" for c in calls), capture_output=True, text=True, check=True)
    text = json.loads(reply.stdout.splitlines()[-1])["result"]["content"][0]["text"]
    emit({"type": "item.completed", "item": {"type": "mcp_tool_call", "server": server, "tool": "create_whitelabel", "result": text}})
sessions = os.path.join(home, "sessions/2026/10/30")
os.makedirs(sessions, exist_ok=True)
with open(os.path.join(sessions, "rollout-%d.jsonl" % time.time_ns()), "w") as rollout:
    rollout.write(json.dumps({"type": "session_meta", "payload": {"cwd": os.getcwd()}}) + "\n")
    rollout.write(json.dumps({"type": "turn_context", "payload": {"cwd": os.getcwd(), "model": "gpt-5-codex"}}) + "\n")
    rollout.write(json.dumps({"type": "event_msg", "payload": {"type": "token_count",
        "info": {"total_token_usage": {"input_tokens": 1000, "output_tokens": 100}},
        "rate_limits": {"limit_id": "codex", "plan_type": "plus",
                        "primary": {"window_minutes": 300, "used_percent": 12.5, "resets_at": 2000000000}}}}) + "\n")
done = "acme" in prompt or "finish up" in prompt
emit({"type": "item.completed", "item": {"type": "agent_message", "text": "Worked on it." + ("\n[[digger:done]]" if done else "")}})
emit({"type": "turn.completed"})
"""
RUNNER_CLAUDE = r"""#!/usr/bin/env python3
import json, os, subprocess, sys
args = sys.argv[1:]
home = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(os.environ["HOME"], ".claude")
if args[:2] == ["auth", "status"]:
    ok = os.path.exists(os.path.join(home, ".credentials.json"))
    print(json.dumps({"loggedIn": ok})); sys.exit(0 if ok else 1)
os.makedirs(home, exist_ok=True)
with open(os.path.join(home, "argv.jsonl"), "a") as log:
    log.write(json.dumps({"argv": args, "cwd": os.getcwd()}) + "\n")
prompt = sys.stdin.read()
emit = lambda value: print(json.dumps(value), flush=True)
emit({"type": "system", "subtype": "init", "session_id": "session-1"})
text = "Here is what I would do." + ("\n[[digger:done]]" if "finish up" in prompt else "")
if "approve" in prompt:   # as Claude would: ask the --permission-prompt-tool before running Bash
    server = json.load(open(args[args.index("--mcp-config") + 1]))["mcpServers"].get("digger")
    decision = "no bridge"
    if server:
        bridge = subprocess.Popen([server["command"], *server["args"]], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        for call in ({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
                     {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "approve", "arguments":
                      {"tool_name": "Bash", "input": {"command": "make deploy"}}}}):
            bridge.stdin.write(json.dumps(call) + "\n")
        bridge.stdin.flush()
        reply = next(r for r in map(json.loads, bridge.stdout) if r.get("id") == 2)
        bridge.stdin.close(); bridge.wait()
        decision = json.loads(reply["result"]["content"][0]["text"])["behavior"]
    text = "decision: " + decision
emit({"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}})
emit({"type": "result", "subtype": "success", "session_id": "session-1"})
"""
WL_TOOL = load("diggers_selftest_tool", {}).SELFTEST_MCP   # a real stdio MCP server


class RunnerBase(unittest.TestCase):
    """Conversations end to end in test mode: the serve loop, fake providers, a real repository."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = pathlib.Path(self.tmp.name)
        self.root = base / "root"
        (self.root / "config").mkdir(parents=True)
        self.root.chmod(0o755)
        repo = self.root / "repos/app"
        (repo / ".grave/mcp").mkdir(parents=True)
        (repo / ".grave/diggers").mkdir()
        (repo / ".grave/mcp/wl.py").write_text(WL_TOOL)
        (repo / ".grave/diggers/wl.md").write_text("White labels live in config/whitelabels/<name>.json.\n")
        (repo / "README").write_text("app\n")
        self.git = lambda *a, cwd=repo: subprocess.run(["git", "-C", str(cwd), "-c", "user.name=t", "-c", "user.email=t@example.test", *a],
                                                       check=True, capture_output=True, text=True).stdout.strip()
        subprocess.run(["git", "init", "-q", "-b", "trunk", str(repo)], check=True)
        self.git("add", "-A"); self.git("commit", "-q", "-m", "init")
        origin = base / "origin.git"
        subprocess.run(["git", "clone", "-q", "--bare", str(repo), str(origin)], check=True)
        self.git("remote", "add", "origin", str(origin)); self.git("fetch", "-q", "origin")
        self.bin = base / "bin"
        self.bin.mkdir()
        for name, text in (("codex", RUNNER_CODEX), ("claude", RUNNER_CLAUDE)):
            (self.bin / name).write_text(text)
            (self.bin / name).chmod(0o755)
        self.owner = base / "owner"
        self.owner.mkdir(mode=0o700)
        self.freeze = base / "freeze"
        self.freeze.write_text("0\n")
        self.env = dict(os.environ, GRAVE_ROOT=str(self.root), GRAVE_DIGGER_TEST="1", HOME=str(self.owner),
                        PATH=f"{self.bin}:{os.environ['PATH']}", GRAVE_FREEZE_FILE=str(self.freeze),
                        GRAVE_CONF=str(base / "grave.conf"), GRAVE_BIN="/bin/true")
        for key in ("CODEX_HOME", "CLAUDE_CONFIG_DIR"):
            self.env.pop(key, None)
        self.loops = []

    def tearDown(self):
        for loop in self.loops:
            if loop.poll() is None:
                loop.terminate()
                loop.wait(timeout=30)
        self.tmp.cleanup()

    def grave(self, *args, ok=True, timeout=60):
        proc = subprocess.run([sys.executable, str(SCRIPT), *args], env=self.env, capture_output=True, text=True,
                              cwd=self.tmp.name, timeout=timeout)
        if ok:
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        return proc

    def serve(self, name, *extra):
        log = open(pathlib.Path(self.tmp.name) / f"serve-{name}.log", "w")   # read it when a runner test fails
        self.addCleanup(log.close)
        loop = subprocess.Popen([sys.executable, str(SCRIPT), "serve", name, *extra], env=self.env,
                                stdout=log, stderr=subprocess.STDOUT)
        self.loops.append(loop)
        status = (self.root / "diggers/.records" / name) if extra else (self.root / "config/diggers" / name / "conversations")
        self.wait(lambda: (status / ".status").exists(), "the loop never wrote its status")
        return loop

    def wait(self, condition, message, seconds=30):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            value = condition()
            if value:
                return value
            time.sleep(.2)
        self.fail(message)

    def records(self, path):
        return {p.stem: json.loads(p.read_text()) for p in path.glob("*.json")}

    def doctor(self):
        return {tuple(line.split("\t", 1)) for line in self.grave("check").stdout.splitlines()}


class RunnerTests(RunnerBase):
    def test_dedicated_codex_conversation_uses_only_the_pinned_tool_and_ends_with_a_verdict(self):
        self.grave("new", "wl", "--repo", "app", "--provider", "codex", "--identity", "dedicated",
                   "--mcp", "wl=python3 .grave/mcp/wl.py", "--check", "test -f config/whitelabels/acme.json")
        self.grave("login", "wl")
        loop = self.serve("wl", "--identity", "dedicated")
        asked = self.grave("ask", "wl", "create white label acme with domain acme.example")
        self.assertIn("Worked on it.", asked.stdout)
        self.assertIn("verdict: ready-for-review", asked.stderr)
        self.assertIn("codex plan quota: 12.5% of the 300-minute window", asked.stderr)
        records = self.root / "diggers/.records/wl"
        (cid, record), = self.records(records).items()
        self.assertEqual((record["state"], record["phase"], record["finished_by"]), ("succeeded", "finished", "done marker"))
        result = record["result"]
        self.assertEqual(result["changes"]["commits"], 1)
        self.assertEqual([c["source"] for c in result["checks"]], ["owner"])
        self.assertEqual(result["quota"]["windows"][0]["window_minutes"], 300)
        self.assertEqual(result["cost"]["model"], "gpt-5-codex")
        self.assertEqual([e["name"] for e in record["events"] if e["type"] == "tool_call"], ["wl__create_whitelabel"])
        home = self.root / "diggers/wl"
        checkout = home / "worktrees" / cid
        self.assertEqual(record["checkout"], str(checkout))
        self.assertEqual(self.git("branch", "--show-current", cwd=checkout), f"digger/wl/{cid}")
        self.assertEqual(self.git("log", "--format=%an", "-1", cwd=checkout), "digger wl")
        self.assertEqual(self.git("status", "--porcelain", cwd=checkout), "")
        (call,) = [json.loads(line) for line in (home / ".codex/argv.jsonl").read_text().splitlines()]
        pin = home / "pins" / cid
        self.assertEqual(call["argv"][1:3], ["-C", str(checkout)])
        self.assertIn(f'mcp_servers.wl.args={json.dumps([str(pin / ".grave/mcp/wl.py")])}', call["argv"])
        instructions = next(a for a in call["argv"] if a.startswith("developer_instructions="))
        self.assertIn("White labels live in config/whitelabels", instructions)
        self.assertIn("[[digger:done]]", instructions)
        self.assertTrue(providers.policy_ok(call["argv"]))
        self.assertFalse(os.access(pin / ".grave/mcp/wl.py", os.W_OK) and os.stat(pin).st_mode & 0o200)
        self.assertIn('+  "name": "acme",', (records / f"{cid}.diff").read_text())
        self.assertEqual(stat.S_IMODE(os.stat(records / f"{cid}.json").st_mode), 0o640)
        self.assertFalse((self.owner / ".codex").exists())   # the owner's login and transcripts untouched
        findings = self.doctor()
        self.assertNotIn("fail", {level for level, _ in findings}, findings)
        self.assertIn(("ok", "digger wl: 1 conversation record(s) valid; checkouts and pinned tools intact;"
                             " every turn audited"), findings)
        # Tampering with the pinned tool is drift doctor names.
        os.chmod(pin / ".grave/mcp", 0o755); os.chmod(pin / ".grave/mcp/wl.py", 0o644)
        (pin / ".grave/mcp/wl.py").write_text("print('pwned')\n")
        self.assertTrue(any(level == "fail" and "pinned tools changed" in message for level, message in self.doctor()))
        # Unmerged work is retained by prune.
        self.assertIn("not merged into the source checkout; retained", self.grave("prune", "wl").stdout)
        self.grave("kill", "wl")
        self.assertEqual(loop.wait(timeout=30), 0)
        self.assertFalse(json.loads((self.root / "config/diggers/wl/digger.json").read_text())["enabled"])
        self.assertIn("is disabled", self.grave("ask", "wl", "again", ok=False).stderr)

    def test_separate_login_claude_conversation_waits_for_a_reply_then_finishes_on_the_marker(self):
        self.grave("new", "wl", "--repo", "app", "--provider", "claude", "--identity", "separate-login",
                   "--mcp", "wl=python3 .grave/mcp/wl.py")
        (self.root / "config/diggers/wl/claude/.credentials.json").write_text("{}")
        self.serve("wl")
        first = self.grave("ask", "wl", "make a white label")
        cid = first.stderr.split()[1]
        self.assertIn("waiting for you: grave digger resume wl " + cid, first.stderr)
        record = json.loads((self.root / f"config/diggers/wl/conversations/{cid}.json").read_text())
        self.assertEqual((record["state"], record["phase"]), ("succeeded", "open"))
        second = self.grave("resume", "wl", cid, "thanks, finish up")
        self.assertIn("verdict: no-changes", second.stderr)
        calls = [json.loads(l)["argv"] for l in (self.root / "config/diggers/wl/claude/argv.jsonl").read_text().splitlines()]
        pin = self.root / "config/diggers/wl/pins" / cid
        self.assertNotIn("--resume", calls[0])
        self.assertEqual(calls[1][-2:], ["--resume", "session-1"])
        for call in calls:
            self.assertEqual(call[call.index("--mcp-config") + 1], str(pin / ".mcp.json"))
            self.assertEqual(call[call.index("--append-system-prompt-file") + 1], str(pin / ".instructions.md"))
        servers = json.loads((pin / ".mcp.json").read_text())["mcpServers"]
        self.assertEqual(servers["wl"]["args"], [str(pin / ".grave/mcp/wl.py")])
        self.assertFalse((self.owner / ".claude").exists())
        self.assertIn("finished; start a new one", self.grave("resume", "wl", cid, "more", ok=False).stderr)
        self.assertIn("checkout removed", self.grave("prune", "wl").stdout)   # an untouched branch is disposable
        meta = json.loads((self.root / f"agents/digger-wl-{cid}/meta.json").read_text())
        self.assertEqual((meta["digger"], meta["pruned"]), ("wl", True))
        self.grave("remove", "wl", "--purge")   # purge leaves no checkout or session history behind
        self.assertEqual(list((self.root / "agents").glob("digger-wl-*")), [])
        self.assertNotIn(f"digger/wl/{cid}", self.git("branch", "--list", "digger/*"))

    def test_cancel_stops_the_turn_and_gaming_mode_requeues_it_once(self):
        self.grave("new", "wl", "--repo", "app", "--provider", "codex", "--identity", "separate-login",
                   "--mcp", "wl=python3 .grave/mcp/wl.py")
        self.grave("login", "wl")
        self.serve("wl")
        conversations = self.root / "config/diggers/wl/conversations"
        asking = subprocess.Popen([sys.executable, str(SCRIPT), "ask", "wl", "sleep on it"], env=self.env,
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        running = lambda: next((r for r in self.records(conversations).values() if r["state"] == "running"), None)
        cid = self.wait(running, "the turn never started")["id"]
        time.sleep(.5)
        self.freeze.write_text("1\n")   # gaming mode
        record = self.wait(lambda: (r := self.records(conversations)[cid])["state"] == "cancelled" and r, "gaming did not stop it")
        self.assertIn("gaming mode entered during run; requeued", record["message"])
        self.assertEqual(asking.wait(timeout=30), 1)
        time.sleep(2)
        self.assertNotEqual(self.records(conversations)[cid]["state"], "running")   # nothing starts while frozen
        self.freeze.write_text("0\n")
        record = self.wait(lambda: (r := self.records(conversations)[cid])["state"] == "running" and r, "requeue never ran")
        self.assertEqual(len(record["requeues"]), 1)
        self.grave("cancel", "wl", cid)
        record = self.wait(lambda: (r := self.records(conversations)[cid])["state"] == "cancelled" and r, "cancel ignored")
        self.assertEqual(record["message"], "Cancelled by the owner")
        self.grave("finish", "wl", cid, ok=False)   # an explicit finish packages what is there
        record = self.records(conversations)[cid]
        self.assertEqual((record["phase"], record["finished_by"], record["result"]["verdict"]), ("finished", "owner", "no-changes"))

    def test_requests_are_validated_queued_and_idempotent(self):
        module = load("diggers_requests", {"GRAVE_ROOT": str(self.root), "GRAVE_DIGGER_TEST": "1"})
        self.grave("new", "wl", "--repo", "app", "--provider", "codex", "--identity", "separate-login")
        definition, _ = module.load("wl")
        cid, rid = module.new_id(), module.new_id()
        self.assertEqual(module.submit(definition, "ask", cid, "hi", request_id=rid), (rid, True))
        self.assertEqual(module.submit(definition, "ask", cid, "hi", request_id=rid), (rid, False))
        self.assertEqual(module.conversation_view(definition, cid)["state"], "queued")
        for op, message, fragment in (("rm", "x", "unknown request"), ("ask", "", "message must be")):
            with self.assertRaises(module.DiggerError) as caught:
                module.submit(definition, op, cid, message)
            self.assertIn(fragment, str(caught.exception))
        for _ in range(module.QUEUE_LIMIT - 1):
            module.submit(definition, "ask", module.new_id(), "more")
        with self.assertRaises(module.DiggerError) as caught:
            module.submit(definition, "ask", module.new_id(), "one too many")
        self.assertIn("requests waiting", str(caught.exception))
        row = module.summary(definition)
        self.assertEqual((row["queue"], row["live"], row["running"]), (module.QUEUE_LIMIT, False, None))
        inbox = self.root / "config/diggers/wl/inbox"
        (inbox / (module.new_id() + ".json")).write_text('{"op": "kill"}')
        os.chmod(next(inbox.glob("*.json")), 0o644)   # a loose mode is refused by the loop, not trusted
        self.serve("wl")
        self.wait(lambda: len(json.loads((self.root / "config/diggers/wl/conversations/.status").read_text())["rejected"]) >= 2,
                  "malformed requests were not refused")

    def test_selftest_runs_both_tiers_end_to_end_and_leaves_nothing_behind(self):
        (self.owner / ".codex").mkdir()
        (self.owner / ".codex/auth.json").write_text("{}")   # the owner's own login
        self.env["GRAVE_DIGGER_TEST_UNITS"] = "1"             # a unit is a background serve
        run = self.grave("selftest", timeout=300)
        lines = [line.split("\t", 1) for line in run.stdout.splitlines()]
        self.assertNotIn("fail", {level for level, _ in lines}, run.stdout + run.stderr)
        for expected in ("selftest-own: verdict ready-for-review", "selftest-own: the pinned MCP tool was called",
                         "selftest: an unlogged digger refuses the request with the login hint",
                         "doctor: no failures for the selftest diggers"):
            self.assertIn(["ok", expected], lines)
        self.assertIn("full dedicated run needs its own login", run.stdout)
        for gone in ("repos/digger-selftest", "config/diggers/selftest", "config/diggers/selftest-own",
                     "diggers/selftest", "diggers/.records/selftest", "worktrees/digger-selftest"):
            self.assertFalse((self.root / gone).exists(), gone)
        self.assertEqual(list((self.root / "agents").glob("digger-selftest*")), [])
        # With a selftest key the dedicated digger logs in and runs the same conversation.
        (self.root / "config/secrets").mkdir(mode=0o700)
        key = self.root / "config/secrets/digger-selftest-key"
        key.write_text("sk-test\n"); key.chmod(0o600)
        run = self.grave("selftest", timeout=300)
        self.assertIn("ok\tselftest: verdict ready-for-review", run.stdout, run.stdout + run.stderr)
        self.assertNotIn("warn\tselftest:", run.stdout)

    def test_definitions_take_checks_and_prompt_chips(self):
        module = load("diggers_optional", {"GRAVE_DIGGER_TEST": "1"})
        value = definition(checks=["npm test"], prompts=[{"label": "New white label", "template": "Create {name} at {domain}"}])
        self.assertEqual(module.validate(value)["checks"], ["npm test"])
        for bad in ([{"label": "x", "template": "{Name}"}], [{"label": "x", "template": "a } b"}], [{"label": "x"}]):
            with self.assertRaises(module.DiggerError):
                module.validate(definition(prompts=bad))
        with self.assertRaises(module.DiggerError):
            module.validate(definition(checks=[""]))


class FakeSlack:
    """Slack's Web API over HTTP and one Socket Mode WebSocket, on loopback."""

    def __init__(self):
        self.calls, self.acks, self.slow, self.lock, self.conn = [], [], 0, threading.Lock(), None
        self.listener = socket.create_server(("127.0.0.1", 0))
        fake = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                method = self.path.rsplit("/", 1)[-1]
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])) or b"{}")
                with fake.lock:
                    fake.calls.append((method, body, self.headers.get("Authorization", "")))
                if method == "chat.postMessage" and fake.slow:
                    time.sleep(fake.slow)
                reply = {"ok": True}
                if method == "apps.connections.open":
                    reply["url"] = f"ws://127.0.0.1:{fake.listener.getsockname()[1]}/link?ticket=t"
                elif method == "auth.test":
                    reply["user_id"] = "UBOT"
                elif method == "chat.postMessage":
                    reply["ts"] = f"{time.time():.6f}"
                data = json.dumps(reply).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.http = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.http.server_address[1]}"
        threading.Thread(target=self.http.serve_forever, daemon=True).start()
        threading.Thread(target=self.accept, daemon=True).start()

    def accept(self):
        while True:
            try:
                conn, _ = self.listener.accept()
            except OSError:
                return
            data = b""
            while b"\r\n\r\n" not in data:
                data += conn.recv(4096)
            key = re.search(rb"Sec-WebSocket-Key: (\S+)", data).group(1).decode()
            accept = base64.b64encode(hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()).decode()
            conn.sendall(("HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                          f"Sec-WebSocket-Accept: {accept}\r\n\r\n").encode())
            self.conn = conn
            self.send({"type": "hello"})
            threading.Thread(target=self.read, args=(conn,), daemon=True).start()

    def read(self, conn):
        buf = b""
        while True:
            try:
                chunk = conn.recv(65536)
            except OSError:
                return
            if not chunk:
                return
            buf += chunk
            while len(buf) >= 6:
                n, at = buf[1] & 0x7F, 2
                if n == 126:
                    n, at = struct.unpack(">H", buf[2:4])[0], 4
                if len(buf) < at + 4 + n:
                    break
                mask, payload, opcode = buf[at:at + 4], buf[at + 4:at + 4 + n], buf[0] & 0x0F
                buf = buf[at + 4 + n:]
                if opcode == 1:
                    text = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
                    with self.lock:
                        self.acks.append(json.loads(text)["envelope_id"])

    def send(self, value):
        data = json.dumps(value).encode()
        header = bytes([0x81]) + (bytes([len(data)]) if len(data) < 126 else bytes([126]) + struct.pack(">H", len(data)))
        self.conn.sendall(header + data)
        return value

    def event(self, user, channel, text, thread=None, kind="app_mention", **extra):
        event = dict({"type": kind, "user": user, "channel": channel, "text": text, "ts": f"{time.time():.6f}"}, **extra)
        if thread:
            event["thread_ts"] = thread
        return self.send({"envelope_id": uuid.uuid4().hex, "type": "events_api",
                          "payload": {"event_id": "Ev" + uuid.uuid4().hex[:10], "event": event}})

    def click(self, user, action, value):
        return self.send({"envelope_id": uuid.uuid4().hex, "type": "interactive", "payload": {
            "type": "block_actions", "user": {"id": user}, "actions": [{"action_id": action, "value": value}]}})

    def posts(self, thread=None):
        with self.lock:
            return [b for m, b, _ in self.calls if m == "chat.postMessage" and thread in (None, b.get("thread_ts"))]

    def called(self, method):
        with self.lock:
            return [b for m, b, _ in self.calls if m == method]

    def close(self):
        self.http.shutdown()
        self.listener.close()
        if self.conn:
            self.conn.close()


TOKENS = "SLACK_APP_TOKEN=xapp-1-A0TEST-0000000000\nSLACK_BOT_TOKEN=xoxb-0000-1111-testtoken\n"


class SlackTests(RunnerBase):
    """Socket Mode intake against a fake Slack: acks, dedupe, allowlists, threads, budgets, approvals, audit."""

    def setUp(self):
        super().setUp()
        self.slack = FakeSlack()
        self.addCleanup(self.slack.close)
        self.env["GRAVE_SLACK_API"] = self.slack.url

    def bind(self, name, **slack):
        path = self.root / f"config/diggers/{name}/digger.json"
        value = json.loads(path.read_text())
        value["slack"] = dict({"channels": ["C1"], "users": ["UALICE"], "dm_policy": "pair", "require_mention": True}, **slack)
        if "budget" in slack:
            value["budget"].update(value["slack"].pop("budget"))
        path.write_text(json.dumps(value))
        self.grave("enable", name)
        proc = subprocess.run([sys.executable, str(SCRIPT), "slack-set", name], input=TOKENS, env=self.env,
                              capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def connected(self, status):
        self.wait(lambda: (json.loads(status.read_text()).get("slack") or {}).get("connected"), "Slack never connected")

    def replied(self, thread, fragment, seconds=30):
        return self.wait(lambda: next((p for p in self.slack.posts(thread) if fragment in p["text"]), None),
                         f"no reply containing {fragment!r} in thread {thread}: {self.slack.posts()}", seconds)

    def test_a_thread_is_one_deduplicated_conversation_and_only_the_allowlist_is_heard(self):
        self.grave("new", "wl", "--repo", "app", "--provider", "codex", "--identity", "dedicated",
                   "--mcp", "wl=python3 .grave/mcp/wl.py", "--check", "test -f config/whitelabels/acme.json")
        self.grave("login", "wl")
        self.bind("wl")
        secret = self.root / "diggers/wl/secrets/slack.env"
        self.assertEqual(stat.S_IMODE(secret.stat().st_mode), 0o600)
        records = self.root / "diggers/.records/wl"
        self.serve("wl", "--identity", "dedicated")
        self.connected(records / ".status")
        self.assertIn("Bearer xapp-", self.slack.calls[[m for m, _, _ in self.slack.calls].index("apps.connections.open")][2])
        # One message arrives as app_mention and as message, and Slack redelivers the envelope.
        ask = self.slack.event("UALICE", "C1", "<@UBOT> create white label acme with domain acme.example")
        thread = ask["payload"]["event"]["ts"]
        self.slack.send(ask)
        self.slack.event("UALICE", "C1", ask["payload"]["event"]["text"], kind="message", ts=thread)
        card = self.replied(thread, "*ready-for-review*")
        self.assertIn("Branch `digger/wl/", card["text"])
        self.assertIn("`test -f config/whitelabels/acme.json` passed", card["text"])
        self.assertTrue(any("Worked on it." in p["text"] and "[[digger:done]]" not in p["text"] for p in self.slack.posts(thread)))
        (record,) = self.records(records).values()
        self.assertEqual(len(record["requests"]), 1)
        self.assertEqual(record["slack"], {"channel": "C1", "thread_ts": thread})
        self.assertEqual(record["requested_by"], "slack:UALICE")
        reactions = {(r["timestamp"], r["name"]) for r in self.slack.called("reactions.add")}
        self.assertEqual(reactions, {(thread, "eyes"), (thread, "white_check_mark")})
        self.assertIn(ask["envelope_id"], self.slack.acks)
        # The ack goes out before any work: here the reply is slow and the ack is not.
        self.slack.slow = 3
        denied = self.slack.event("UBOB", "C1", "<@UBOT> make me one too")
        self.wait(lambda: denied["envelope_id"] in self.slack.acks, "no ack while the reply was slow", seconds=1.5)
        self.slack.slow = 0
        self.replied(denied["payload"]["event"]["ts"], "not on this digger's allowlist")
        # Bots, unbound channels and unmentioned top-level messages are never requests.
        for sent in (self.slack.event("UALICE", "C1", "<@UBOT> hi", bot_id="B1"),
                     self.slack.event("UALICE", "C9", "<@UBOT> hi"),
                     self.slack.event("UALICE", "C1", "just chatting", kind="message")):
            self.wait(lambda: sent["envelope_id"] in self.slack.acks, "unacked envelope")
        # A follow-up in a finished thread needs no mention and is answered with the reason.
        self.slack.event("UALICE", "C1", "and another", kind="message", thread=thread)
        self.replied(thread, "this conversation has finished")
        time.sleep(1)
        self.assertEqual(len(self.records(records)), 1)
        # A stranger's DM gets a pairing code; the owner confirms it.
        dm = self.slack.event("UCAROL", "D1", "hello", kind="message", channel_type="im")
        code = re.search(r"grave digger pair wl (\w+)", self.replied(dm["payload"]["event"]["ts"], "grave digger pair")["text"]).group(1)
        self.assertIn("UCAROL paired", self.grave("pair", "wl", code).stdout)
        self.assertIn("UCAROL", json.loads((self.root / "config/diggers/wl/digger.json").read_text())["slack"]["users"])
        self.assertIn("no such pairing code", self.grave("pair", "wl", "000000", ok=False).stderr)
        # Every turn and every refusal has an audit line, without message text.
        audit = [json.loads(line) for line in (records / ".audit.jsonl").read_text().splitlines()]
        self.assertEqual([a["outcome"] for a in audit], ["succeeded", "denied", "refused"])
        self.assertEqual((audit[0]["slack_user"], audit[0]["thread"], audit[1]["slack_user"]), ("UALICE", thread, "UBOB"))
        self.assertNotIn("acme", (records / ".audit.jsonl").read_text())
        findings = self.doctor()
        self.assertNotIn("fail", {level for level, _ in findings}, findings)
        for expected in ("digger wl: Slack Socket Mode connected (outbound only, nothing listens)",
                         "digger wl: Slack tokens stored 0600, readable only by the identity that serves it"):
            self.assertIn(("ok", expected), findings)
        self.grave("slack-logout", "wl")
        self.assertFalse(secret.exists())

    def test_budget_pauses_the_digger_answers_in_thread_pages_once_and_resumes(self):
        self.grave("new", "wl", "--repo", "app", "--provider", "codex", "--identity", "separate-login")
        self.grave("login", "wl")
        self.bind("wl", budget={"daily_turns": 1})
        status = self.root / "config/diggers/wl/conversations/.status"
        pages = pathlib.Path(self.tmp.name) / "pages"
        notify = pathlib.Path(self.tmp.name) / "notify"
        notify.write_text(f"#!/bin/sh\necho \"$@\" >> {pages}\n"); notify.chmod(0o755)
        self.env["GRAVE_BIN"] = str(notify)
        self.serve("wl")
        self.connected(status)
        first = self.slack.event("UALICE", "C1", "<@UBOT> hello there")
        self.replied(first["payload"]["event"]["ts"], "Worked on it.")
        second = self.slack.event("UALICE", "C1", "<@UBOT> one more")
        self.replied(second["payload"]["event"]["ts"], "daily budget is spent")
        self.wait(lambda: (lambda v: v["pause"] == "budget" and "budget" in v["conditions"])(json.loads(status.read_text())),
                  "the loop never reported the budget pause")
        module = load("diggers_pages", {"GRAVE_ROOT": str(self.root), "GRAVE_DIGGER_TEST": "1", "GRAVE_BIN": str(notify)})
        definition, _ = module.load("wl")
        row = module.summary(definition)
        self.assertEqual((row["pause"], row["today"]["turns"], row["limits"]["turns"]), ("budget", 1, 1))
        self.assertTrue(row["slack"]["connected"])
        module.notify_finished({})
        module.notify_finished({})
        self.assertEqual(pages.read_text().count("daily budget spent"), 1)   # once per condition per hour
        self.grave("resume-budget", "wl")
        self.wait(lambda: json.loads(status.read_text())["pause"] is None, "resume-budget did not clear the pause")
        third = self.slack.event("UALICE", "C1", "<@UBOT> now?")
        self.replied(third["payload"]["event"]["ts"], "Worked on it.")

    def test_claude_asks_an_allowlisted_approver_in_the_thread(self):
        self.grave("new", "wl", "--repo", "app", "--provider", "claude", "--identity", "separate-login",
                   "--mcp", "wl=python3 .grave/mcp/wl.py")
        (self.root / "config/diggers/wl/claude/.credentials.json").write_text("{}")
        self.bind("wl", users=["UALICE", "UBOB"], approvers=["UALICE"])
        settings = json.loads((self.root / "config/diggers/wl/policy/claude-settings.json").read_text())
        self.assertEqual(settings["permissions"]["defaultMode"], "default")   # asks the prompt tool instead of refusing
        self.serve("wl")
        self.connected(self.root / "config/diggers/wl/conversations/.status")
        ask = self.slack.event("UALICE", "C1", "<@UBOT> please approve a deploy")
        thread = ask["payload"]["event"]["ts"]
        prompt = self.wait(lambda: next((p for p in self.slack.posts(thread) if p.get("blocks")), None), "no approval prompt")
        self.assertIn("make deploy", prompt["blocks"][0]["text"]["text"])
        aid = prompt["blocks"][1]["elements"][0]["value"]
        self.slack.click("UBOB", "approve", aid)   # a user, not an approver: ignored
        time.sleep(1.5)
        self.assertFalse(self.slack.called("chat.update"))
        self.slack.click("UALICE", "approve", aid)
        self.replied(thread, "decision: allow")
        self.assertIn("Approved by <@UALICE>", self.slack.called("chat.update")[0]["text"])
        (call,) = [json.loads(l)["argv"] for l in (self.root / "config/diggers/wl/claude/argv.jsonl").read_text().splitlines()]
        self.assertEqual(call[call.index("--permission-prompt-tool") + 1], "mcp__digger__approve")
        self.assertTrue(providers.policy_ok(call))
        # A dashboard conversation has no thread to ask in, so its prompt is denied at once.
        self.assertIn("decision: deny", self.grave("ask", "wl", "approve from the dashboard", timeout=60).stdout)
        outcomes = [json.loads(l)["outcome"] for l in (self.root / "config/diggers/wl/conversations/.audit.jsonl").read_text().splitlines()]
        self.assertEqual(outcomes, ["approved", "succeeded", "approval-denied", "succeeded"])


class SlackUnitTests(unittest.TestCase):
    def setUp(self):
        self.module = load("diggers_slack_unit", {"GRAVE_DIGGER_TEST": "1"})
        self.slack = self.module.slackbot

    def test_bindings_are_deny_by_default_and_approvers_are_users(self):
        good = {"channels": ["C1"], "users": ["U1"], "dm_policy": "deny", "require_mention": True}
        self.assertEqual(self.module.validate(definition(slack=good))["slack"], good)
        for bad, fragment in (({**good, "approvers": ["U2"]}, "subset"), ({**good, "users": ["alice"]}, "Slack IDs"),
                              ({**good, "dm_policy": "open"}, "dm_policy"), ({**good, "extra": 1}, "slack must hold"),
                              ({**good, "require_mention": "yes"}, "require_mention")):
            with self.assertRaises(self.module.DiggerError) as caught:
                self.module.validate(definition(slack=bad))
            self.assertIn(fragment, str(caught.exception))
        with self.assertRaises(self.module.DiggerError):
            self.module.validate(definition(tools={"mcp": {"digger": {"command": "x", "args": []}},
                                                   "allow": ["mcp__digger__*"], "deny": []}))
        codex = definition(slack=good)
        self.assertFalse(providers.approvals(codex))   # Codex stays deny-only
        claude = definition(provider="claude", identity="separate-login", work="worktree", slack=good)
        argv, _ = self.module.compile_command(claude)
        self.assertEqual(argv[argv.index("--permission-prompt-tool") + 1], "mcp__digger__approve")
        argv, _ = self.module.compile_command(dict(claude, slack={**good, "approvers": []}))
        self.assertNotIn("--permission-prompt-tool", argv)

    def test_tokens_parse_from_either_form_and_ids_are_stable(self):
        values = self.slack.parse_tokens("xoxb-0000-1111-testtoken\nxapp-1-A0TEST-0000000000\n")
        self.assertEqual(self.slack.parse_tokens(self.slack.render_tokens(values)), values)
        with self.assertRaises(self.slack.SlackError):
            self.slack.parse_tokens("xoxb-0000-1111-testtoken")
        a = self.slack.thread_id("C1", "1700000000.000100")
        self.assertEqual(a, self.slack.thread_id("C1", "1700000000.000100"))
        self.assertRegex(a, r"^1700000000-[0-9a-f]{32}$")
        self.assertNotEqual(a, self.slack.thread_id("C2", "1700000000.000100"))

    def test_unanswered_approvals_expire_and_a_disconnected_channel_denies(self):
        tmp = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(tmp))
        answers, updates = [], []

        class Runner:
            definition = {"name": "wl", "slack": {"channels": [], "users": ["U1"], "dm_policy": "deny", "require_mention": True}}
            approvals = str(tmp)
            record = staticmethod(lambda cid: {"slack": {"channel": "C1", "thread_ts": "1.0"}})
            answer = staticmethod(lambda path, allow, why: (answers.append((pathlib.Path(path).stem, allow, why)),
                                                             pathlib.Path(path).with_suffix(".answer").write_text("{}")))

        channel = self.slack.Channel(Runner(), {"SLACK_APP_TOKEN": "x", "SLACK_BOT_TOKEN": "y"}, threading.Event())
        channel.update = lambda c, ts, text: updates.append(text)
        (tmp / "old.json").write_text(json.dumps({"id": "old", "conversation": "c", "tool": "Bash", "input": {}, "at": time.time() - 700}))
        channel.state["connected"] = True
        channel.asked["old"] = ("C1", "2.0", "Bash")
        channel.approvals(tmp)
        self.assertEqual(answers, [("old", False, "expired after 10 minutes without an approver")])
        self.assertIn("Expired, denied", updates[0])
        (tmp / "new.json").write_text(json.dumps({"id": "new", "conversation": "c", "tool": "Bash", "input": {}, "at": time.time()}))
        channel.state["connected"] = False
        channel.approvals(tmp)
        self.assertEqual(answers[-1][:2], ("new", False))


class InstallContractTests(unittest.TestCase):
    def test_cli_dispatches_digger_and_a_root_half_without_a_new_sudo_rule(self):
        self.assertIn('python3 "$GRAVE_ROOT/scripts/diggers.py" "$@"', GRAVE)
        self.assertIn('python3 -I "$GRAVE_ROOT/scripts/diggers.py" __root "$@"', GRAVE)
        self.assertIn('[[ $EUID == 0 ]] || { bad "internal digger helper requires root"; exit 1; }', GRAVE)
        self.assertIn("grave digger new <n> --repo <r>", GRAVE.split("cmd_help() {")[1])
        self.assertIn('"sudo", "-n", GRAVE, "__digger"', SCRIPT.read_text())
        self.assertNotIn("digger", (ROOT / "libexec/install-owner.sh").read_text())
        self.assertNotIn("diggers", RAISE.split("sudoers_content=")[1].split("\n")[1])

    def test_doctor_reports_digger_logins_as_warnings_and_everything_else_as_failures(self):
        self.assertIn('python3 "$GRAVE_ROOT/scripts/diggers.py" check', GRAVE)
        self.assertIn('warn) warn "$message" ;;', GRAVE)
        self.assertIn('fail) fail "$message"', GRAVE)
        self.assertIn("digger_report", GRAVE.split("cmd_doctor() {")[1])

    def test_raise_installs_runner_and_units_and_only_the_cli_enables_them(self):
        self.assertIn('install -m 755 "$REPO_DIR/libexec/diggers.py" "$GRAVE_ROOT/scripts/diggers.py"', RAISE)
        self.assertIn('install -m 644 "$REPO_DIR/libexec/digger-slack.py" "$GRAVE_ROOT/scripts/digger-slack.py"', RAISE)
        help_text = GRAVE.split("cmd_help() {")[1]
        for verb in ("grave digger slack-set <n>", "slack-logout <n>", "grave digger pair <n> <code>", "resume-budget <n>"):
            self.assertIn(verb, help_text)
        for unit, serve in (("gravedecay-digger@", "serve %i --identity dedicated"), ("gravedecay-digger-owner@", "serve %i")):
            self.assertIn(f"install_unit {unit}.service", RAISE)
            self.assertNotIn(f"enable_restart {unit}", RAISE)
            text = (ROOT / f"systemd/{unit}.service.tmpl").read_text()
            for line in ("NoNewPrivileges=yes", "ProtectSystem=strict", "UMask=0077", "MemoryMax=8G", "CPUQuota=400%",
                         f"ExecStart=@PYTHON@ -I @GRAVE_ROOT@/scripts/diggers.py {serve}\n", "WantedBy=multi-user.target"):
                self.assertIn(line, text, unit)
            self.assertNotRegex(text, r"(?m)^(After|Wants|Requires|BindsTo|PartOf)=.*t3code", unit)
        self.assertIn('sudo systemctl try-restart "$unit"', RAISE)
        self.assertIn('"systemctl", "enable", wanted', SCRIPT.read_text())
        dedicated = (ROOT / "systemd/gravedecay-digger@.service.tmpl").read_text()
        self.assertIn("User=grave-digger-%i", dedicated)
        self.assertIn("ProtectHome=yes", dedicated)
        self.assertIn("ReadWritePaths=@GRAVE_ROOT@/diggers/%i @GRAVE_ROOT@/diggers/.records/%i\n", dedicated)
        self.assertNotIn("@HOME@", dedicated)
        self.assertIn("User=@USER@", (ROOT / "systemd/gravedecay-digger-owner@.service.tmpl").read_text())
        self.assertIn("gravedecay-digger@.service gravedecay-digger-owner@.service", (ROOT / "uninstall.sh").read_text())

    def test_login_terminal_keeps_the_load_bearing_auth_prefix(self):
        webterm = (ROOT / "bin/webterm").read_text()
        self.assertIn('auth-digger-*) [[ "$WORKSPACE_ROLE" == admin ]]', webterm)
        self.assertIn('cmd=(grave digger login "${name#auth-digger-}")', webterm)

    def test_no_listener_and_no_new_port(self):
        source = SCRIPT.read_text()
        for needle in ("import socket", "http.server", "socketserver", ".bind(", ".listen("):
            self.assertNotIn(needle, source)
        slack = (ROOT / "libexec/digger-slack.py").read_text()   # Socket Mode dials out; it never accepts
        for needle in ("http.server", "socketserver", ".bind(", ".listen(", "create_server", ".accept("):
            self.assertNotIn(needle, slack)
        self.assertIn('raise SlackError("Socket Mode URL must be wss://")', slack)
        self.assertNotIn("digger", (ROOT / "docs/PORTS.md").read_text().lower())

    def test_workspace_slugs_cannot_take_the_digger_account_namespace(self):
        self.assertIn("slug prefix 'digger-' is reserved", (ROOT / "bin/grave-workspaces").read_text())

    def test_backups_leave_per_digger_logins_out_by_default(self):
        self.assertIn("--exclude='config/diggers/*/codex' --exclude='config/diggers/*/claude'", GRAVE)


if __name__ == "__main__":
    unittest.main()
