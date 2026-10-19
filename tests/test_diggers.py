"""Diggers M1: definitions, identity tiers, per-digger login, compiled policy and doctor."""
import importlib.util
import json
import os
import pathlib
import stat
import subprocess
import sys
import tempfile
import unittest
import unittest.mock

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

    def test_raise_installs_runner_and_units_but_never_starts_them(self):
        self.assertIn('install -m 755 "$REPO_DIR/libexec/diggers.py" "$GRAVE_ROOT/scripts/diggers.py"', RAISE)
        for unit in ("gravedecay-digger@", "gravedecay-digger-owner@"):
            self.assertIn(f"install_unit {unit}.service", RAISE)
            self.assertNotIn(f"enable_restart {unit}", RAISE)
            text = (ROOT / f"systemd/{unit}.service.tmpl").read_text()
            for line in ("NoNewPrivileges=yes", "ProtectSystem=strict", "UMask=0077", "MemoryMax=8G", "CPUQuota=400%",
                         "ExecStart=@PYTHON@ -I @GRAVE_ROOT@/scripts/diggers.py run %i"):
                self.assertIn(line, text, unit)
            self.assertNotIn("[Install]", text, unit)
        dedicated = (ROOT / "systemd/gravedecay-digger@.service.tmpl").read_text()
        self.assertIn("User=grave-digger-%i", dedicated)
        self.assertIn("ProtectHome=yes", dedicated)
        self.assertIn("ReadWritePaths=@GRAVE_ROOT@/diggers/%i", dedicated)
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
        self.assertNotIn("digger", (ROOT / "docs/PORTS.md").read_text().lower())

    def test_workspace_slugs_cannot_take_the_digger_account_namespace(self):
        self.assertIn("slug prefix 'digger-' is reserved", (ROOT / "bin/grave-workspaces").read_text())

    def test_backups_leave_per_digger_logins_out_by_default(self):
        self.assertIn("--exclude='config/diggers/*/codex' --exclude='config/diggers/*/claude'", GRAVE)


if __name__ == "__main__":
    unittest.main()
