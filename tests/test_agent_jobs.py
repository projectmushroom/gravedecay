"""Real Git/process lifecycle; fake providers never contact an LLM service."""
import datetime as dt
import importlib.util
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import threading
import urllib.request
import urllib.error
import unittest
from unittest.mock import patch

from test_dashboard import load_dashboard

ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "libexec/agent-jobs.py"


class AgentJobTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.repo = self.root / "repos/project"; self.repo.mkdir(parents=True)
        self.bin = self.root / "bin"; self.bin.mkdir()
        self.home = self.root / "home"; self.home.mkdir()
        self.store = self.root / "config/secrets/agent-jobs"
        self.prompt = self.root / "prompt.txt"; self.prompt.write_text('Review $(touch PWNED); `echo nope` and "quoted text".\n')
        self.env = dict(os.environ, HOME=str(self.home), GRAVE_ROOT=str(self.root), MULTI_USER="0",
            PATH=str(self.bin) + ":" + str(Path(sys.executable).parent) + ":" + os.environ["PATH"],
            GIT_AUTHOR_NAME="Test", GIT_AUTHOR_EMAIL="test@example.test",
            GIT_COMMITTER_NAME="Test", GIT_COMMITTER_EMAIL="test@example.test")
        self.exe("systemctl", 'import os,sys\nfrom pathlib import Path\nsys.exit(3 if (Path(os.environ["GRAVE_ROOT"])/"gaming").exists() else 0)\n')
        self.exe("notify", 'import json,os,sys\nfrom pathlib import Path\nwith (Path(os.environ["GRAVE_ROOT"])/"notifications").open("a") as f: f.write(json.dumps(sys.argv[1:])+"\\n")\n')
        self.env["GRAVE_BIN"] = str(self.bin / "notify")
        for agent in ("codex", "claude"):
            self.exe(agent, '''import json, os, sys, time
from pathlib import Path
root=Path(os.environ["GRAVE_ROOT"])
text=sys.stdin.read()
with (root/"calls").open("a") as f: f.write(json.dumps({"argv":sys.argv[1:],"prompt":text,"cwd":os.getcwd()})+"\\n")
print("provider started", flush=True)
(root/"started").touch()
mode=os.environ.get("FAKE_AGENT", "success")
if mode=="wait":
    while True: time.sleep(.1)
if mode=="fail": sys.exit(9)
Path("result.txt").write_text("finished")
print("<script>literal output</script>", flush=True)
''')
        for command in (("init",), ("add", "."), ("commit", "-m", "base")):
            if command[0] == "add":
                (self.repo / "file.txt").write_text("base\n")
            subprocess.run(["git", "-C", str(self.repo), *command], env=self.env,
                           check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        (self.root / "scripts").mkdir()
        shutil.copy(HELPER, self.root / "scripts/agent-jobs.py")
        self.conf = self.root / "grave.conf"
        self.socket = "jobs-test-" + self.root.name
        self.conf.write_text("GRAVE_ROOT=" + shlex.quote(str(self.root)) + "\nTMUX_SOCKET=" + self.socket + "\nTMUX_CONF=/dev/null\n")
        self.env["GRAVE_CONF"] = str(self.conf)
        if shutil.which("tmux"):
            self.addCleanup(subprocess.run, ["tmux", "-L", self.socket, "kill-server"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def exe(self, name, code):
        path = self.bin / name; path.write_text("#!" + sys.executable + "\n" + code); path.chmod(0o755)

    def call(self, *args, ok=True, env=None):
        result = subprocess.run([sys.executable, str(HELPER), *args], env=env or self.env,
                                capture_output=True, text=True, timeout=15)
        if ok:
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        else:
            self.assertNotEqual(result.returncode, 0)
        return result

    def add(self, name="nightly", *args):
        self.call("run", name, "--prompt-file", str(self.prompt), "--repo", "project", *args)

    def records(self, name="nightly"):
        return [json.loads(path.read_text()) for path in sorted((self.store / name / "runs").glob("*.json"))]

    def job(self, name="nightly"):
        return json.loads((self.store / name / "job.json").read_text())

    def wait_for(self, predicate):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if predicate(): return
            time.sleep(.05)
        self.fail("worker did not reach expected state")

    def background(self, action="worker", *args):
        command = [sys.executable, str(HELPER), action, *(args or (() if action == "scheduler" else ("nightly",)))]
        proc = subprocess.Popen(command, env=dict(self.env, FAKE_AGENT="wait"), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        def stop():
            if proc.poll() is None: proc.terminate()
            proc.wait(timeout=15)
        self.addCleanup(stop)
        return proc

    def test_literal_prompt_is_private_and_runs_once_in_a_fresh_worktree(self):
        self.add()
        self.prompt.write_text("changed after scheduling")
        self.call("worker", "nightly"); self.call("worker", "nightly")
        calls = [json.loads(line) for line in (self.root / "calls").read_text().splitlines()]
        self.assertEqual(len(calls), 1)
        self.assertIn('$(touch PWNED)', calls[0]["prompt"])
        self.assertNotIn("changed after scheduling", calls[0]["prompt"])
        self.assertEqual(calls[0]["argv"], ["exec", "--sandbox", "workspace-write", "--color", "never", "-"])
        record = self.records()[0]
        self.assertEqual((record["status"], record["exit_code"]), ("succeeded", 0))
        directory = Path(record["dir"])
        self.assertEqual(directory.parent, self.root / "worktrees/project")
        self.assertTrue((directory / "result.txt").exists())
        self.assertFalse((directory / "PWNED").exists())
        self.assertFalse((self.repo / "result.txt").exists())
        history = self.root / "agents" / record["session"]
        self.assertEqual(json.loads((history / "meta.json").read_text())["scheduled_job"], "nightly")
        self.assertIn("literal output", (history / record["log"]).read_text())
        for path in (self.store / "nightly/prompt.txt", self.store / "nightly/job.json", history / record["log"]):
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertIn("Scheduled nightly: succeeded", (self.root / "notifications").read_text())
        self.call("check")

    def test_claude_failure_is_recorded_and_not_retried(self):
        self.add("nightly", "--agent", "claude")
        self.call("worker", "nightly", env=dict(self.env, FAKE_AGENT="fail"))
        self.assertEqual(self.records()[0]["status"], "failed")
        self.assertEqual(self.records()[0]["exit_code"], 9)
        self.assertEqual(json.loads((self.root / "calls").read_text())["argv"], ["-p"])
        self.assertIsNone(self.job()["next_due"])

    def test_duplicate_workers_cannot_overlap_and_cancel_stops_live_work(self):
        self.add(); proc = self.background()
        self.wait_for(lambda: (self.root / "started").exists())
        self.call("worker", "nightly")
        self.assertEqual(len(self.records()), 1)
        self.assertEqual(len((self.root / "calls").read_text().splitlines()), 1)
        if shutil.which("tmux") and shutil.which("jq"):
            result = subprocess.run([str(ROOT / "bin/grave"), "agents", "prune"], env=self.env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("scheduled run active; retained", result.stdout)
        self.call("jobs", "cancel", "nightly")
        proc.wait(timeout=10)
        self.assertEqual(self.records()[0]["status"], "cancelled")
        self.assertFalse(self.job()["enabled"])
        self.assertTrue(Path(self.records()[0]["dir"]).exists())

    def test_gaming_mode_skips_due_jobs_without_thawing(self):
        self.add("nightly", "--on", "Mon 02:00")
        path = self.store / "nightly/job.json"; value = self.job(); value["next_due"] = time.time() - 60
        path.write_text(json.dumps(value))
        (self.root / "gaming").touch()
        self.call("worker", "nightly")
        self.assertEqual(self.records()[0]["status"], "skipped")
        self.assertGreater(self.job()["next_due"], time.time())
        self.assertFalse((self.root / "calls").exists())
        self.assertFalse((self.root / "worktrees").exists())

    def test_entering_gaming_mode_cancels_an_active_run(self):
        self.add(); proc = self.background()
        self.wait_for(lambda: (self.root / "started").exists())
        (self.root / "gaming").touch()
        proc.wait(timeout=10)
        self.assertEqual(self.records()[0]["reason"], "gaming mode entered during run")

    def test_scheduler_launches_due_work_and_shutdown_records_cancellation(self):
        self.add(); proc = self.background("scheduler")
        self.wait_for(lambda: (self.root / "started").exists())
        proc.terminate(); proc.wait(timeout=15)
        self.assertEqual(self.records()[0]["status"], "cancelled")
        self.assertEqual(len((self.root / "calls").read_text().splitlines()), 1)

    def test_validation_rejects_duplicate_names_bad_times_and_unsafe_prompt(self):
        for args in (("--at", "25:00"), ("--on", "Mon 02:00; touch PWNED"), ("--timeout", "0")):
            self.call("run", "bad", "--prompt-file", str(self.prompt), "--repo", "project", *args, ok=False)
        self.add()
        self.call("run", "nightly", "--prompt-file", str(self.prompt), "--repo", "project", ok=False)
        path = self.store / "nightly/prompt.txt"; path.chmod(0o644)
        self.call("check", ok=False)
        self.call("worker", "nightly", ok=False)
        self.assertFalse((self.root / "calls").exists())
        path.chmod(0o600); path.unlink(); path.symlink_to(self.prompt)
        self.call("check", ok=False)
        self.call("jobs", env=dict(self.env, MULTI_USER="1"), ok=False)

    def test_dashboard_report_excludes_prompts_and_is_owner_gated(self):
        self.add(); self.call("worker", "nightly")
        dash = load_dashboard(dict(self.env, GRAVEDECAY_ALLOWED_USERS="owner@example.test"))
        report = dash.collect_scheduled_runs()
        self.assertEqual(report["runs"][0]["status"], "succeeded")
        self.assertIn("literal output", report["runs"][0]["tail"])
        self.assertNotIn("prompt", json.dumps(report))
        self.assertNotIn("PWNED", json.dumps(report))
        dash._state = lambda headers: {"tmux": [], "agent_history": []}
        self.assertNotIn("scheduled", dash.state({}))
        self.assertIn("scheduled", dash.state({"Tailscale-User-Login": "owner@example.test"}))

    def module(self):
        with patch.dict(os.environ, self.env):
            spec = importlib.util.spec_from_file_location("job_probe", HELPER)
            module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
        return module

    def test_calendar_timeout_and_interruption_reconciliation(self):
        module = self.module()
        after = dt.datetime(2026, 9, 7, 2, 0).timestamp()  # Monday at the configured time
        self.assertEqual(dt.datetime.fromtimestamp(module.next_due("Mon 02:00", after)), dt.datetime(2026, 9, 14, 2, 0))
        self.assertEqual(dt.datetime.fromtimestamp(module.next_due("02:00", after)), dt.datetime(2026, 9, 8, 2, 0))
        self.add("nightly", "--timeout", "60")
        now = time.time() + 1
        with patch.dict(os.environ, dict(self.env, FAKE_AGENT="wait")), patch.object(module.time, "time", side_effect=[now, now + 61, now + 62]):
            module.worker("nightly")
        self.assertEqual(self.records()[0]["status"], "timed-out")
        path = next((self.store / "nightly/runs").glob("*.json"))
        value = json.loads(path.read_text()); value["status"] = "running"; path.write_text(json.dumps(value))
        self.call("check", ok=False)
        module.recover("nightly")
        self.assertEqual(self.records()[0]["status"], "interrupted")
        self.assertIsNone(self.job()["next_due"])
        self.call("check")

    def test_cancel_api_requires_owner_and_validates_literal_job_name(self):
        self.add()
        dash = load_dashboard(dict(self.env, GRAVEDECAY_ALLOWED_USERS="owner@example.test"))
        calls = []
        dash.sh = lambda command, timeout: (calls.append(command) or (0, "cancelled", ""))
        server = dash.ThreadingHTTPServer(("127.0.0.1", 0), dash.Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        try:
            def post(data, headers):
                request = urllib.request.Request(f"http://127.0.0.1:{server.server_port}/api/agent-job-cancel", data=json.dumps(data).encode(), headers=headers)
                try:
                    with urllib.request.urlopen(request, timeout=3) as response:
                        return response.status
                except urllib.error.HTTPError as error:
                    return error.code
            self.assertEqual(post({"name":"nightly"}, {}), 403)
            owner = {"Tailscale-User-Login":"owner@example.test"}
            self.assertEqual(post({"name":"../../other"}, owner), 400)
            self.assertEqual(post({"name":"nightly"}, {**owner,"Sec-Fetch-Site":"cross-site"}), 403)
            self.assertEqual(calls, [])
            self.assertEqual(post({"name":"nightly"}, owner), 200)
            self.assertEqual(calls[0], [dash.GRAVE, "agents", "jobs", "cancel", "nightly"])
        finally:
            server.shutdown(); server.server_close(); thread.join()

    def test_doctor_and_installer_share_the_managed_runner_contract(self):
        unit = (ROOT / "systemd/gravedecay-agents.service.tmpl").read_text()
        self.assertIn("User=@USER@", unit); self.assertIn("Environment=HOME=@HOME@", unit)
        self.assertIn("KillMode=control-group", unit)
        for path in ("bin/grave", "raise.sh", "uninstall.sh"):
            self.assertIn("gravedecay-agents.service", (ROOT / path).read_text())
        self.assertIn("agent-jobs.py", (ROOT / "raise.sh").read_text())
