"""Dispatch real worktrees/tmux sessions using a fake CLI, never a paid agent."""
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from test_inbox_archive import load_dashboard

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(shutil.which("tmux") and shutil.which("jq"), "tmux and jq required")
class DispatchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.repo = self.root / "repos/project"
        self.repo.mkdir(parents=True)
        scripts = self.root / "scripts"
        scripts.mkdir()
        shutil.copy(ROOT / "libexec/agent-task.py", scripts / "agent-task.py")
        self.socket = "dispatch-" + self.root.name
        self.addCleanup(subprocess.run, ["tmux", "-L", self.socket, "kill-server"],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.conf = self.root / "grave.conf"
        self.conf.write_text("\n".join([
            "GRAVE_ROOT=" + shlex.quote(str(self.root)), "TMUX_SOCKET=" + self.socket,
            "TMUX_CONF=/dev/null"]) + "\n")
        bindir = self.root / "bin"
        bindir.mkdir()
        self.capture = self.root / "capture.jsonl"
        for agent in ("codex", "claude"):
            p = bindir / agent
            p.write_text("#!" + sys.executable + "\n" + '''import json, os, sys
with open(os.environ["DISPATCH_CAPTURE"], "a") as f:
    f.write(json.dumps({"argv":sys.argv, "cwd":os.getcwd(), "tty":sys.stdin.isatty()}) + "\\n")
print("FAKE_AGENT_RAN", flush=True)
''')
            p.chmod(0o755)
        self.env = patch.dict(os.environ, {
            "PATH": str(bindir) + ":" + str(Path(sys.executable).parent) + ":" + os.environ["PATH"],
            "GRAVE_CONF": str(self.conf), "DISPATCH_CAPTURE": str(self.capture),
            "GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "test@example.com",
            "GIT_COMMITTER_NAME": "Test", "GIT_COMMITTER_EMAIL": "test@example.com"})
        self.env.start()
        self.addCleanup(self.env.stop)
        for args in (("init",), ("add", "."), ("commit", "--allow-empty", "-m", "base")):
            subprocess.run(["git", "-C", str(self.repo), *args], check=True,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.dash = load_dashboard(self.root)
        self.dash.GRAVE = str(ROOT / "bin/grave")
        # Only the actual tmux subprocesses use a private test socket. The
        # single-owner platform gate still sees the production 'agents' socket.
        real_sh = self.dash.sh
        def isolated_sh(cmd, **kwargs):
            if cmd[:3] == ["tmux", "-L", "agents"]:
                cmd = ["tmux", "-L", self.socket, *cmd[3:]]
            return real_sh(cmd, **kwargs)
        self.dash.sh = isolated_sh
        self.issue = {"id": "uuid-one", "identifier": "GRV-108", "title": "Fix \"quotes\" and $variables",
                      "description": "Literal text\n$(touch " + str(self.root / "INJECTED") + ")\n`false`",
                      "url": "https://linear.app/grave/issue/GRV-108/fix"}
        self.dash.linear_key = lambda: "fake-key"
        self.dash.linear_gql = lambda payload: {"issue": dict(self.issue)}
        self.request = {"issue": "GRV-108", "repo": "project", "agent": "codex"}

    def launch(self):
        status, response = self.dash.dispatch_linear(dict(self.request))
        self.assertEqual(status, 201, response)
        name = response["session"]["name"]
        result = self.root / "agents" / name / "task-result.json"
        for _ in range(100):
            if result.exists() and json.loads(result.read_text()).get("status") == "exited":
                break
            time.sleep(.05)
        self.assertTrue(result.exists(), response)
        self.assertEqual(json.loads(result.read_text()).get("status"), "exited")
        return name

    def test_launch_passes_literal_issue_prompt_in_isolated_worktree(self):
        name = self.launch()
        call = json.loads(self.capture.read_text().splitlines()[0])
        self.assertEqual(len(call["argv"]), 2)
        self.assertIn("GRV-108", call["argv"][1])
        self.assertIn("$(touch", call["argv"][1])
        self.assertEqual(call["cwd"], str(self.root / "worktrees/project" / name))
        self.assertTrue(call["tty"])
        self.assertFalse((self.root / "INJECTED").exists())
        meta = self.dash.agent_worktree_metadata(name)
        self.assertEqual(meta["dispatch"]["issue"]["id"], "GRV-108")
        self.assertEqual(meta["dispatch"]["status"], "exited")
        self.assertNotIn("description", meta["dispatch"]["issue"])
        self.dash.ALLOWED_USERS = {"owner@example.test"}
        self.dash._state = lambda headers: {"tmux": [{"name": name}], "agent_history": []}
        self.assertIn("dispatch", self.dash.state({"Tailscale-User-Login": "owner@example.test"})["tmux"][0])
        self.assertNotIn("dispatch", self.dash.state({"Tailscale-User-Login": "viewer@example.test"})["tmux"][0])
        task = self.root / "agents" / name / "task.json"
        self.assertEqual(task.stat().st_mode & 0o777, 0o600)
        self.assertEqual(json.loads(task.read_text())["issue"]["description"], self.issue["description"])
        logs = list(task.parent.glob("session-*.log"))
        self.assertTrue(logs)
        self.assertIn("FAKE_AGENT_RAN", logs[0].read_text())
        self.assertEqual(subprocess.run(["tmux", "-L", self.socket, "has-session", "-t", "=" + name]).returncode, 0)

    def test_duplicate_taps_and_retries_reuse_the_same_session(self):
        name = self.launch()
        with ThreadPoolExecutor(max_workers=2) as pool:
            replies = list(pool.map(lambda _: self.dash.dispatch_linear(dict(self.request)), range(2)))
        for status, response in replies:
            self.assertIn(status, (200, 409))
            if status == 200:
                self.assertEqual(response["session"]["name"], name)
                self.assertTrue(response["existing"])
        self.assertEqual(len(self.capture.read_text().splitlines()), 1)
        meta_path = self.root / "agents" / name / "meta.json"
        meta = json.loads(meta_path.read_text())
        meta["pruned"] = True
        meta_path.write_text(json.dumps(meta))
        self.assertEqual(self.dash.dispatch_linear(self.request)[0], 409)

    def test_simultaneous_initial_requests_create_only_one_agent(self):
        with ThreadPoolExecutor(max_workers=2) as pool:
            replies = list(pool.map(lambda _: self.dash.dispatch_linear(dict(self.request)), range(2)))
        self.assertEqual(sum(code == 201 for code, _ in replies), 1, replies)
        self.assertTrue(all(code in (200, 201, 409) for code, _ in replies), replies)
        for _ in range(100):
            if self.capture.exists():
                break
            time.sleep(.05)
        self.assertEqual(len(self.capture.read_text().splitlines()), 1)

    def test_claude_uses_the_same_interactive_launch_path(self):
        self.request["agent"] = "claude"
        self.launch()
        call = json.loads(self.capture.read_text().splitlines()[0])
        self.assertTrue(call["argv"][0].endswith("claude"))
        self.assertEqual(len(call["argv"]), 2)

    def test_invalid_requests_and_symlink_repos_do_not_launch(self):
        (self.root / "repos/link").symlink_to(self.repo, target_is_directory=True)
        for change in ({"repo": "../project"}, {"repo": "link"}, {"agent": "sh"},
                       {"issue": "GRV-108; id"}, {"prompt": "client-controlled"}):
            with self.subTest(change=change):
                status, _ = self.dash.dispatch_linear(dict(self.request, **change))
                self.assertEqual(status, 400)
        self.assertFalse((self.root / "agents").exists())

    def test_api_failure_or_oversized_issue_leaves_no_worktree(self):
        for issue in (None, dict(self.issue, identifier="OTHER-1"),
                      dict(self.issue, description="x" * 70000), dict(self.issue, url="javascript:alert(1)")):
            self.dash.linear_gql = lambda payload: {"issue": issue}
            code, result = self.dash.dispatch_linear(dict(self.request))
            self.assertEqual(code, 502, result)
            self.assertFalse((self.root / "worktrees").exists())
        self.dash.linear_gql = lambda payload: (_ for _ in ()).throw(ValueError("secret-failure"))
        code, result = self.dash.dispatch_linear(dict(self.request))
        self.assertEqual(code, 502)
        self.assertNotIn("secret-failure", result["output"])
        self.assertFalse(self.dash.DISPATCH_LOCK.locked())

    def test_no_agent_and_unsupported_platforms_refuse_launch(self):
        with patch.object(self.dash.shutil, "which", return_value=None):
            self.assertEqual(self.dash.dispatch_linear(self.request)[0], 409)
        for flag in ("MACOS", "PORTABLE", "REQUIRE_BACKEND_TOKEN"):
            with patch.object(self.dash, flag, True):
                self.assertEqual(self.dash.dispatch_linear(self.request)[0], 404)
        self.assertFalse((self.root / "worktrees").exists())

    def test_pr_lookup_uses_the_session_branch_and_source_repository(self):
        name = self.launch()
        calls = []
        def fake(cmd, **kwargs):
            calls.append(cmd)
            if cmd[0] == "git":
                return 0, "git@github.com:acme/project.git\n", ""
            return 0, json.dumps([{"number": 19, "url": "https://github.com/acme/project/pull/19", "state": "OPEN"}]), ""
        self.dash.sh = fake
        self.assertEqual(self.dash.dispatch_pr(name)["number"], 19)
        self.assertIn("agent/" + name, calls[-1])
        self.assertIn("acme/project", calls[-1])
        self.dash.dispatch_pr(name)
        self.assertEqual(len(calls), 2)  # cached, not another GitHub poll

    def test_task_privacy_is_checked_before_new_worktree(self):
        task = self.root / "task.json"
        task.write_text(json.dumps({"agent": "codex", "issue": {"id": "GRV-108", "title": "x", "description": "", "url": self.issue["url"]}}))
        task.chmod(0o644)
        p = subprocess.run([str(ROOT / "bin/grave"), "agents", "new", "unsafe", "--repo", "project", "--task", str(task)], capture_output=True, text=True)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("private", p.stderr)
        self.assertFalse((self.root / "worktrees").exists())

    def test_doctor_checks_saved_tasks_and_terminal_resumes_the_worktree(self):
        name = self.launch()
        source = (ROOT / "bin/grave").read_text().split("\ncmd_agents()", 1)[0]
        def doctor():
            return subprocess.run(["bash"], input=source + "\nagent_tasks_check", text=True, capture_output=True)
        self.assertEqual(doctor().returncode, 0)
        task = self.root / "agents" / name / "task.json"
        task.chmod(0o644)
        self.assertNotEqual(doctor().returncode, 0)
        task.chmod(0o600)
        original = task.read_text()
        task.write_text(original.replace('"agent": "codex"', '"agent": "claude"'))
        self.assertNotEqual(doctor().returncode, 0)
        task.write_text(original)
        subprocess.run(["tmux", "-L", self.socket, "kill-session", "-t", "=" + name], check=True)
        real_tmux = shutil.which("tmux")
        wrapper = self.root / "bin/tmux"
        wrapper.write_text('#!/bin/bash\ncase " $* " in *" new-session -A "*) exit 0 ;; esac\nexec '
                           + shlex.quote(real_tmux) + ' "$@"\n')
        wrapper.chmod(0o755)
        (self.root / "bin/grave").symlink_to(ROOT / "bin/grave")
        env = dict(os.environ, GRAVE_ROOT=str(self.root), TMUX_SOCKET=self.socket)
        p = subprocess.run([str(ROOT / "bin/webterm"), name], env=env, capture_output=True, text=True)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        cwd = subprocess.check_output([real_tmux, "-L", self.socket, "display-message", "-p", "-t", "=" + name + ":", "#{pane_current_path}"], text=True).strip()
        self.assertEqual(cwd, str(self.root / "worktrees/project" / name))
        self.assertEqual(len(self.capture.read_text().splitlines()), 1)  # no resubmission
        subprocess.run([real_tmux, "-L", self.socket, "kill-session", "-t", "=" + name], check=True)
        (self.root / "agents" / name / "meta.json").write_text("broken")
        self.assertNotEqual(subprocess.run([str(ROOT / "bin/webterm"), name], env=env,
                                          capture_output=True).returncode, 0)


class DispatchHTTPTests(unittest.TestCase):
    def test_identity_csrf_and_backend_gates_precede_dispatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            dash = load_dashboard(tmp)
            dash.ALLOWED_USERS = {"owner@example.test"}
            calls = []
            dash.dispatch_linear = lambda data: (calls.append(data) or (201, {"ok": True}))
            server = dash.ThreadingHTTPServer(("127.0.0.1", 0), dash.Handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                origin = "http://127.0.0.1:" + str(server.server_port)
                def post(headers):
                    return urllib.request.urlopen(urllib.request.Request(origin + "/api/linear-dispatch", data=b'{}', headers=headers), timeout=3)
                for headers in ({"Tailscale-User-Login": "viewer@example.test"},
                                {"Tailscale-User-Login": "owner@example.test", "Sec-Fetch-Site": "cross-site"}):
                    with self.assertRaises(urllib.error.HTTPError) as err:
                        post(headers)
                    self.assertEqual(err.exception.code, 403)
                self.assertEqual(calls, [])
                with self.assertRaises(urllib.error.HTTPError) as err:
                    urllib.request.urlopen(urllib.request.Request(origin + "/api/linear-dispatch",
                        data=b"x" * 2049, headers={"Tailscale-User-Login": "owner@example.test"}), timeout=3)
                self.assertEqual(err.exception.code, 413)
                with self.assertRaises(urllib.error.HTTPError) as err:
                    urllib.request.urlopen(urllib.request.Request(origin + "/api/dispatch-pr?name=session",
                        headers={"Tailscale-User-Login": "viewer@example.test"}), timeout=3)
                self.assertEqual(err.exception.code, 403)
                with post({"Tailscale-User-Login": "owner@example.test"}) as response:
                    self.assertEqual(response.status, 201)
                dash.REQUIRE_BACKEND_TOKEN = True
                dash.BACKEND_TOKEN = "fixture"
                with self.assertRaises(urllib.error.HTTPError):
                    post({})
                self.assertEqual(len(calls), 1)
            finally:
                server.shutdown()
                server.server_close()
                thread.join()
