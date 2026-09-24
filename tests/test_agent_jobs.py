"""Real Git/process lifecycle; fake providers never contact an LLM service."""
import datetime as dt
import importlib.util
import itertools
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
PROVIDERS = ROOT / "libexec/providers.py"


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    return module


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
        # A check that records where it ran and exits with FAKE_CHECK.
        self.exe("fakecheck", '''import json, os, sys
from pathlib import Path
with (Path(os.environ["GRAVE_ROOT"])/"checked").open("a") as f: f.write(json.dumps({"cwd": os.getcwd()})+"\\n")
print("check output line", flush=True)
sys.exit(int(os.environ.get("FAKE_CHECK", "0")))
''')
        for agent in ("codex", "claude"):
            self.exe(agent, '''import json, os, re, subprocess, sys, time
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
if os.environ.get("FAKE_TRANSCRIPT"):
    # Claude Code files transcripts under a directory named after the cwd; Codex under dated rollouts.
    cwd=os.getcwd(); home=Path(os.environ["HOME"])
    project=home/".claude/projects"/re.sub(r"[^A-Za-z0-9]", "-", cwd); project.mkdir(parents=True)
    def line(model, cwd, n):
        return json.dumps({"type":"assistant","cwd":cwd,"timestamp":"2026-09-24T02:00:00Z","requestId":"req"+n,
            "message":{"id":"msg"+n,"model":model,"usage":{"input_tokens":1000,"output_tokens":100}}})
    (project/"s.jsonl").write_text(line("claude-sonnet-4-5", cwd, "1")+"\\n"+line("claude-x", cwd, "2")+"\\n"+line("claude-x", cwd, "2")+"\\n")
    decoy=home/".claude/projects/-elsewhere"; decoy.mkdir(parents=True)
    (decoy/"d.jsonl").write_text(line("claude-opus-4-1", "/elsewhere", "3")+"\\n")
    rollouts=home/".codex/sessions/2026/09/24"; rollouts.mkdir(parents=True)
    (rollouts/"rollout-2026-09-24T02-00-00-abc.jsonl").write_text("\\n".join([
        json.dumps({"type":"session_meta","payload":{"id":"abc","cwd":cwd}}),
        json.dumps({"type":"turn_context","payload":{"cwd":cwd,"model":"gpt-x"}}),
        json.dumps({"type":"event_msg","payload":{"type":"token_count","info":{"total_token_usage":{"input_tokens":2000,"cached_input_tokens":1000,"output_tokens":100}}}})])+"\\n")
    (rollouts/"rollout-2026-09-24T01-00-00-def.jsonl").write_text(json.dumps({"type":"session_meta","payload":{"id":"def","cwd":"/elsewhere"}})+"\\n"
        + json.dumps({"type":"event_msg","payload":{"type":"token_count","info":{"total_token_usage":{"input_tokens":9000,"cached_input_tokens":0,"output_tokens":9000}}}})+"\\n")
if mode=="clean": sys.exit(0)
Path("result.txt").write_text("finished\\n")
if mode=="commit":
    subprocess.run(["git","add","result.txt"], check=True)
    subprocess.run(["git","commit","-q","-m","agent work"], check=True)
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
                                capture_output=True, text=True, timeout=30)
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
        self.assertEqual(calls[0]["argv"], load(PROVIDERS, "providers_probe").job_command("codex")[1:])
        self.assertEqual(calls[0]["argv"][:3], ["exec", "-c", 'sandbox_mode="workspace-write"'])
        record = self.records()[0]
        self.assertEqual((record["status"], record["exit_code"]), ("succeeded", 0))
        directory = Path(record["dir"])
        self.assertEqual(directory.parent, self.root / "worktrees/project")
        self.assertTrue((directory / "result.txt").exists())
        self.assertFalse((directory / "PWNED").exists())
        self.assertFalse((self.repo / "result.txt").exists())
        history = self.root / "agents" / record["session"]
        self.assertEqual(json.loads((history / "meta.json").read_text())["scheduled_job"], "nightly")
        self.assertEqual(record["base"], json.loads((history / "meta.json").read_text())["base"])
        self.assertIn("literal output", (history / record["log"]).read_text())
        for path in (self.store / "nightly/prompt.txt", self.store / "nightly/job.json", history / record["log"]):
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertIn("Scheduled nightly: succeeded", (self.root / "notifications").read_text())
        # Uncommitted work is still work to review; nothing to check was detected.
        self.assertEqual(record["result"], {"verdict": "ready-for-review", "checks": [], "cost": None,
            "changes": {"commits": 0, "files": 0, "insertions": 0, "deletions": 0, "dirty": True}})
        self.assertIsNone(self.job()["checks"])
        self.call("check"); self.call("check", "results")

    def test_claude_failure_is_recorded_and_not_retried(self):
        self.add("nightly", "--agent", "claude")
        self.call("worker", "nightly", env=dict(self.env, FAKE_AGENT="fail"))
        self.assertEqual(self.records()[0]["status"], "failed")
        self.assertEqual(self.records()[0]["exit_code"], 9)
        self.assertEqual(self.records()[0]["result"]["verdict"], "provider-failed")
        self.assertEqual(json.loads((self.root / "calls").read_text())["argv"], ["-p"])
        self.assertIsNone(self.job()["next_due"])

    def test_committing_provider_with_passing_checks_is_ready_for_review(self):
        self.add("nightly", "--check", str(self.bin / "fakecheck"), "--check", "echo second check")
        self.assertEqual(self.job()["checks"], [str(self.bin / "fakecheck"), "echo second check"])
        self.call("worker", "nightly", env=dict(self.env, FAKE_AGENT="commit", FAKE_TRANSCRIPT="1"))
        record = self.records()[0]
        self.assertEqual((record["status"], record["exit_code"]), ("succeeded", 0))
        result = record["result"]
        self.assertEqual(result["verdict"], "ready-for-review")
        self.assertEqual(result["changes"], {"commits": 1, "files": 1, "insertions": 1, "deletions": 0, "dirty": False})
        self.assertEqual([c["cmd"] for c in result["checks"]], [str(self.bin / "fakecheck"), "echo second check"])
        self.assertEqual([c["exit_code"] for c in result["checks"]], [0, 0])
        self.assertIn("check output line", result["checks"][0]["tail"])
        self.assertIn("second check", result["checks"][1]["tail"])
        self.assertTrue(all(c["seconds"] >= 0 for c in result["checks"]))
        self.assertEqual(json.loads((self.root / "checked").read_text())["cwd"], record["dir"])
        transcript = (self.root / "agents" / record["session"] / record["log"]).read_text()
        self.assertIn("$ echo second check\nsecond check\n\n(exit 0, ", transcript)
        # Spend: only transcripts whose cwd is this worktree; unknown models bill at the top price.
        sonnet, top = (1000 * 3 + 100 * 15) / 1e6, (1000 * 15 + 100 * 75) / 1e6
        codex = (1000 * 15 + 1000 * 1.5 + 100 * 75) / 1e6
        self.assertEqual(result["cost"], {"usd": round(sonnet + top + codex, 4), "estimated": True, "model": "gpt-x"})
        self.assertEqual((self.store / "nightly/job.json").stat().st_mode & 0o777, 0o600)
        self.call("check", "results")
        self.assertIn("last succeeded/ready-for-review", self.call("jobs").stdout)
        self.assertEqual(json.loads(self.call("jobs", "--json").stdout)[0]["last_verdict"], "ready-for-review")

    def test_failing_check_is_checks_failed_without_changing_lifecycle_status(self):
        self.add("nightly", "--check", str(self.bin / "fakecheck"))
        self.call("worker", "nightly", env=dict(self.env, FAKE_AGENT="commit", FAKE_CHECK="3"))
        record = self.records()[0]
        self.assertEqual((record["status"], record["exit_code"]), ("succeeded", 0))
        self.assertEqual(record["result"]["verdict"], "checks-failed")
        self.assertEqual(record["result"]["checks"][0]["exit_code"], 3)
        self.assertEqual(record["result"]["changes"]["commits"], 1)
        self.assertIn("Scheduled nightly: succeeded", (self.root / "notifications").read_text())
        self.call("check", "results")

    def test_clean_tree_without_commits_is_no_changes_and_skips_checks(self):
        self.add("nightly", "--check", str(self.bin / "fakecheck"))
        self.call("worker", "nightly", env=dict(self.env, FAKE_AGENT="clean"))
        record = self.records()[0]
        self.assertEqual(record["status"], "succeeded")
        self.assertEqual(record["result"]["verdict"], "no-changes")
        self.assertEqual(record["result"]["changes"], {"commits": 0, "files": 0, "insertions": 0, "deletions": 0, "dirty": False})
        self.assertEqual(record["result"]["checks"], [])
        self.assertFalse((self.root / "checked").exists())

    def test_checks_are_detected_from_the_project_when_none_are_scheduled(self):
        module = self.module()
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            self.assertEqual(module.detect_checks(project), [])
            (project / "package.json").write_text(json.dumps({"scripts": {"test": 'echo "Error: no test specified" && exit 1'}}))
            self.assertEqual(module.detect_checks(project), [])
            (project / "package.json").write_text(json.dumps({"scripts": {"test": "vitest run"}}))
            self.assertEqual(module.detect_checks(project), ["npm test"])
            (project / "package.json").unlink()
            (project / "pyproject.toml").write_text("[project]\nname='x'\n")
            self.assertEqual(module.detect_checks(project), ["pytest"])
            (project / "pyproject.toml").unlink()
            (project / "Makefile").write_text("build:\n\ttrue\ntest:\n\ttrue\n")
            self.assertEqual(module.detect_checks(project), ["make test"])
            (project / "Makefile").write_text("build:\n\ttrue\n")
            self.assertEqual(module.detect_checks(project), [])
        self.exe("npm", 'import json,os,sys\nfrom pathlib import Path\n(Path(os.environ["GRAVE_ROOT"])/"npm-argv").write_text(json.dumps(sys.argv[1:]))\nprint("1 passing")\n')
        (self.repo / "package.json").write_text(json.dumps({"scripts": {"test": "vitest run"}}))
        subprocess.run(["git", "-C", str(self.repo), "add", "."], env=self.env, check=True)
        subprocess.run(["git", "-C", str(self.repo), "commit", "-q", "-m", "node"], env=self.env, check=True)
        self.add()
        self.call("worker", "nightly", env=dict(self.env, FAKE_AGENT="commit"))
        result = self.records()[0]["result"]
        self.assertEqual(result["verdict"], "ready-for-review")
        self.assertEqual((result["checks"][0]["cmd"], result["checks"][0]["exit_code"]), ("npm test", 0))
        self.assertIn("1 passing", result["checks"][0]["tail"])
        self.assertEqual(json.loads((self.root / "npm-argv").read_text()), ["test"])

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
        self.assertEqual(self.records()[0]["result"]["verdict"], "cancelled")
        self.assertFalse(self.job()["enabled"])
        self.assertTrue(Path(self.records()[0]["dir"]).exists())

    def test_gaming_mode_skips_due_jobs_without_thawing(self):
        self.add("nightly", "--on", "Mon 02:00")
        path = self.store / "nightly/job.json"; value = self.job(); value["next_due"] = time.time() - 60
        path.write_text(json.dumps(value))
        (self.root / "gaming").touch()
        self.call("worker", "nightly")
        self.assertEqual(self.records()[0]["status"], "skipped")
        self.assertEqual(self.records()[0]["result"], {"verdict": "skipped", "changes": None, "checks": [], "cost": None})
        self.assertGreater(self.job()["next_due"], time.time())
        self.assertFalse((self.root / "calls").exists())
        self.assertFalse((self.root / "worktrees").exists())
        self.call("check", "results")

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
        for args in (("--at", "25:00"), ("--on", "Mon 02:00; touch PWNED"), ("--timeout", "0"), ("--check", ""), ("--check", "x" * 501)):
            self.call("run", "bad", "--prompt-file", str(self.prompt), "--repo", "project", *args, ok=False)
        self.assertFalse((self.store / "bad").exists())
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
        self.assertEqual(report["runs"][0]["result"]["verdict"], "ready-for-review")
        self.assertIn("literal output", report["runs"][0]["tail"])
        self.assertNotIn("prompt", json.dumps(report))
        self.assertNotIn("PWNED", json.dumps(report))
        dash._state = lambda headers: {"tmux": [], "agent_history": []}
        self.assertNotIn("scheduled", dash.state({}))
        self.assertIn("scheduled", dash.state({"Tailscale-User-Login": "owner@example.test"}))

    def module(self):
        with patch.dict(os.environ, self.env):
            module = load(HELPER, "job_probe")
        return module

    def test_calendar_timeout_and_interruption_reconciliation(self):
        module = self.module()
        after = dt.datetime(2026, 9, 7, 2, 0).timestamp()  # Monday at the configured time
        self.assertEqual(dt.datetime.fromtimestamp(module.next_due("Mon 02:00", after)), dt.datetime(2026, 9, 14, 2, 0))
        self.assertEqual(dt.datetime.fromtimestamp(module.next_due("02:00", after)), dt.datetime(2026, 9, 8, 2, 0))
        self.add("nightly", "--timeout", "60")
        now = time.time() + 1
        clock = itertools.chain([now], itertools.repeat(now + 61))
        with patch.dict(os.environ, dict(self.env, FAKE_AGENT="wait")), patch.object(module.time, "time", side_effect=lambda: next(clock)):
            module.worker("nightly")
        self.assertEqual(self.records()[0]["status"], "timed-out")
        self.assertEqual(self.records()[0]["result"]["verdict"], "timed-out")
        path = next((self.store / "nightly/runs").glob("*.json"))
        value = json.loads(path.read_text()); value["status"] = "running"; path.write_text(json.dumps(value))
        self.call("check", ok=False)
        module.recover("nightly")
        self.assertEqual(self.records()[0]["status"], "interrupted")
        self.assertEqual(self.records()[0]["result"]["verdict"], "interrupted")
        self.assertIsNone(self.job()["next_due"])
        self.call("check"); self.call("check", "results")

    def test_check_budget_is_the_run_timeout(self):
        module = self.module()
        self.exe("slowcheck", 'import os, time\nfrom pathlib import Path\nprint("still running", flush=True)\n(Path(os.environ["GRAVE_ROOT"])/"checking").touch()\ntime.sleep(30)\n')
        self.add("nightly", "--timeout", "60", "--check", str(self.bin / "slowcheck"))
        real = time.time
        # The provider finishes inside the budget; the clock jumps past it once the check is running.
        clock = lambda: real() + (61 if (self.root / "checking").exists() else 0)
        with patch.dict(os.environ, dict(self.env, FAKE_AGENT="commit")), patch.object(module.time, "time", side_effect=clock):
            module.worker("nightly")
        record = self.records()[0]
        self.assertEqual((record["status"], record["exit_code"]), ("timed-out", 0))
        self.assertEqual(record["result"]["verdict"], "timed-out")
        check = record["result"]["checks"][0]
        self.assertNotEqual(check["exit_code"], 0)
        self.assertIn("still running", check["tail"])
        self.assertIn("[check stopped: configured runtime limit reached]", check["tail"])

    def test_doctor_requires_a_verdict_and_recovery_backfills_older_records(self):
        module = self.module()
        self.add(); self.call("worker", "nightly", env=dict(self.env, FAKE_AGENT="commit"))
        path = next((self.store / "nightly/runs").glob("*.json"))
        value = json.loads(path.read_text())
        legacy = {k: v for k, v in value.items() if k not in ("result", "base")}
        path.write_text(json.dumps(legacy))
        self.call("check")
        self.assertIn("no valid verdict", self.call("check", "results", ok=False).stderr)
        module.recover("nightly")
        self.assertEqual(self.records()[0]["result"], {"verdict": "ready-for-review", "checks": [], "cost": None,
            "changes": {"commits": 1, "files": 1, "insertions": 1, "deletions": 0, "dirty": False}})
        self.call("check", "results")
        for broken in ({"verdict": "green"}, {**value["result"], "verdict": "succeeded"}, {**value["result"], "checks": [{"cmd": "x"}]}):
            path.write_text(json.dumps({**value, "result": broken}))
            self.call("check", "results", ok=False)
        shutil.rmtree(value["dir"])
        path.write_text(json.dumps({**legacy, "status": "failed"}))
        module.recover("nightly")
        self.assertEqual(self.records()[0]["result"]["verdict"], "provider-failed")
        path.write_text(json.dumps(legacy))
        module.recover("nightly")
        # Without a worktree to inspect, a human decides.
        self.assertEqual(self.records()[0]["result"], {"verdict": "ready-for-review", "changes": None, "checks": [], "cost": None})

    def test_unknown_models_bill_at_the_most_expensive_known_price(self):
        dash = load_dashboard(dict(self.env, GRAVEDECAY_ALLOWED_USERS="owner@example.test"))
        self.assertEqual(dash.claude_price("claude-sonnet-4-5"), (3, 15))
        self.assertEqual(dash.claude_price("claude-opus-4-1"), (15, 75))
        self.assertEqual(dash.claude_price("claude-next"), max(price for _, price in dash.CLAUDE_PRICES))
        self.assertEqual(dash.claude_price(None), (15, 75))
        self.assertIsNone(dash.transcript_cost(str(self.root / "nowhere")))

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

    def test_one_provider_table_and_sandbox_policy_serve_jobs_dispatch_and_keeper(self):
        providers = load(PROVIDERS, "providers_contract")
        self.assertEqual(providers.SANDBOX, {"job": "workspace-write", "dispatch": "workspace-write", "keeper": "read-only"})
        job, dispatch = providers.job_command("codex"), providers.dispatch_command("codex", "task text")
        self.assertEqual(job[:4], ["codex", "exec", "-c", 'sandbox_mode="workspace-write"'])
        self.assertEqual(dispatch, ["codex", "-c", 'sandbox_mode="workspace-write"', "task text"])
        self.assertEqual(providers.dispatch_command("claude", "task text"), ["claude", "task text"])
        keeper = load(ROOT / "dashboard/keeper.py", "keeper_contract")
        runner = keeper.Runner("/tmp/x", "h", script="/srv/dev/scripts/keeper.py", grave="/usr/local/bin/grave",
                               api="http://127.0.0.1:4712/grave", work_dir="/srv/dev/config/keeper")
        record = {"provider": "codex", "model": "", "session_id": None}
        self.assertEqual(runner.command(record), providers.keeper_command("codex", runner.mcp_command(), runner.work_dir))
        self.assertIn('sandbox_mode="read-only"', runner.command(record))
        for argv in (job, dispatch, providers.job_command("claude"), runner.command(record),
                     runner.command({"provider": "claude", "model": "", "session_id": None})):
            self.assertTrue(providers.policy_ok(argv), argv)
        self.assertFalse(providers.policy_ok(["codex", "--dangerously-bypass-approvals-and-sandbox"]))
        # Only the table spells a sandbox; consumers cannot drift from it.
        for path in ("libexec/agent-jobs.py", "libexec/agent-task.py", "dashboard/keeper.py"):
            source = (ROOT / path).read_text()
            self.assertNotIn("sandbox_mode", source, path); self.assertNotIn("--sandbox", source, path)
            self.assertIn("providers", source, path)

    def test_doctor_and_installer_share_the_managed_runner_contract(self):
        unit = (ROOT / "systemd/gravedecay-agents.service.tmpl").read_text()
        self.assertIn("User=@USER@", unit); self.assertIn("Environment=HOME=@HOME@", unit)
        self.assertIn("KillMode=control-group", unit)
        for path in ("bin/grave", "raise.sh", "uninstall.sh"):
            self.assertIn("gravedecay-agents.service", (ROOT / path).read_text())
        raise_sh = (ROOT / "raise.sh").read_text()
        self.assertIn("agent-jobs.py", raise_sh)
        self.assertIn('install -m 644 "$REPO_DIR/libexec/providers.py" "$GRAVE_ROOT/scripts/providers.py"', raise_sh)
        grave = (ROOT / "bin/grave").read_text()
        self.assertIn('check "scheduled run records carry a valid verdict" env GRAVE_ROOT="$GRAVE_ROOT" python3 "$GRAVE_ROOT/scripts/agent-jobs.py" check results', grave)
