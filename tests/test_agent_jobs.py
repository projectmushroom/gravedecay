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
        self.env = dict(os.environ, HOME=str(self.home), GRAVE_ROOT=str(self.root), MULTI_USER="0", FIXTURES=str(ROOT / "tests/fixtures/quota"),
            GRAVE_FREEZE_FILE=str(self.root / "cgroup.freeze"),
            PATH=str(self.bin) + ":" + str(Path(sys.executable).parent) + ":" + os.environ["PATH"],
            GIT_AUTHOR_NAME="Test", GIT_AUTHOR_EMAIL="test@example.test",
            GIT_COMMITTER_NAME="Test", GIT_COMMITTER_EMAIL="test@example.test")
        self.exe("systemctl", 'import os,sys\nfrom pathlib import Path\n'
                 'if sys.argv[1] == "show": print("After=" + os.environ.get("FAKE_AFTER", "network.target") + "\\nRequires=\\nWants=\\nBindsTo=\\nRequisite=\\nPartOf=\\n"); sys.exit(0)\n'
                 'sys.exit(3 if "t3code" in sys.argv else 0)\n')
        self.freeze = self.root / "cgroup.freeze"
        self.exe("notify", 'import json,os,sys\nfrom pathlib import Path\nwith (Path(os.environ["GRAVE_ROOT"])/"notifications").open("a") as f: f.write(json.dumps(sys.argv[1:])+"\\n")\n')
        self.env["GRAVE_BIN"] = str(self.bin / "notify")
        # A check that records where it ran and exits with FAKE_CHECK.
        self.exe("fakecheck", '''import json, os, sys
from pathlib import Path
with (Path(os.environ["GRAVE_ROOT"])/"checked").open("a") as f: f.write(json.dumps({"cwd": os.getcwd(), "base": os.environ.get("GRAVE_BASE")})+"\\n")
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
fixtures=Path(os.environ["FIXTURES"])
if os.environ.get("FAKE_QUOTA"):
    # The run's own codex exec rollout, in the shape codex-cli 0.160.0 writes (tests/fixtures/quota).
    cwd=os.getcwd(); rollouts=Path(os.environ["HOME"])/".codex/sessions/2026/10/06"; rollouts.mkdir(parents=True, exist_ok=True)
    (rollouts/("rollout-2026-10-06T02-00-00-"+os.environ["FAKE_QUOTA"]+".jsonl")).write_text((fixtures/("rollout-"+os.environ["FAKE_QUOTA"]+".jsonl")).read_text().replace("__CWD__", cwd))
if mode=="ratelimit":
    # Claude Code refused with a usage-limit 429: the transcript records the reset time, the CLI exits 1.
    cwd=os.getcwd(); project=Path(os.environ["HOME"])/".claude/projects"/re.sub(r"[^A-Za-z0-9]", "-", cwd); project.mkdir(parents=True, exist_ok=True)
    text=(fixtures/"claude-429.jsonl").read_text().replace("__CWD__", cwd).replace("1790200800", os.environ.get("FAKE_RESETS", "1790200800"))
    (project/"c429.jsonl").write_text(text)
    print("You've hit your limit", flush=True); sys.exit(1)
if mode=="clean": sys.exit(0)
if mode=="weaken":
    Path("tests/test_a.py").unlink()
    Path("tests/test_b.py").write_text("import pytest\\n@pytest.mark.skip\\ndef test_b(): pass\\n")
    Path("package-lock.json").write_text("{}\\n")
    subprocess.run(["git","add","-A"], check=True)
    subprocess.run(["git","commit","-q","-m","weaken"], check=True)
    sys.exit(0)
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
        # Uncommitted work is work to review, but nothing checked it: the run is unverified and says so.
        self.assertEqual(record["result"], {"verdict": "unverified", "checks": [], "cost": None,
            "changes": {"commits": 0, "files": 0, "insertions": 0, "deletions": 0, "dirty": True}})
        notification = (self.root / "notifications").read_text()
        self.assertIn("Scheduled nightly: succeeded, unverified", notification)
        self.assertIn("No checks ran", notification)
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
        self.assertEqual([c["source"] for c in result["checks"]], ["owner", "owner"])
        self.assertIn("check output line", result["checks"][0]["tail"])
        self.assertIn("second check", result["checks"][1]["tail"])
        self.assertTrue(all(c["seconds"] >= 0 for c in result["checks"]))
        self.assertEqual(json.loads((self.root / "checked").read_text()), {"cwd": record["dir"], "base": record["base"]})
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
        self.assertIn("Scheduled nightly: succeeded, checks-failed", (self.root / "notifications").read_text())
        self.call("check", "results")
        # Reconciliation never rewrites a record that already carries a valid verdict.
        self.module().recover("nightly")
        self.assertEqual(self.records()[0], record)

    def test_clean_tree_without_commits_is_no_changes_and_skips_checks(self):
        self.add("nightly", "--check", str(self.bin / "fakecheck"))
        self.call("worker", "nightly", env=dict(self.env, FAKE_AGENT="clean"))
        record = self.records()[0]
        self.assertEqual(record["status"], "succeeded")
        self.assertEqual(record["result"]["verdict"], "no-changes")
        self.assertEqual(record["result"]["changes"], {"commits": 0, "files": 0, "insertions": 0, "deletions": 0, "dirty": False})
        self.assertEqual(record["result"]["checks"], [])
        self.assertFalse((self.root / "checked").exists())

    def commit(self, message="base"):
        subprocess.run(["git", "-C", str(self.repo), "add", "-A"], env=self.env, check=True)
        subprocess.run(["git", "-C", str(self.repo), "commit", "-q", "--allow-empty", "-m", message], env=self.env, check=True)
        return subprocess.run(["git", "-C", str(self.repo), "rev-parse", "HEAD"], env=self.env, check=True,
                              capture_output=True, text=True).stdout.strip()

    def test_test_changes_are_informational_capped_and_counted_into_the_budget(self):
        module = self.module()
        (self.repo / "tests").mkdir(); (self.repo / ".github/workflows").mkdir(parents=True); (self.repo / "__snapshots__").mkdir()
        (self.repo / "tests/test_a.py").write_text("def test_a():\n    assert 1\n    assert 2\n")
        (self.repo / "tests/test_b.py").write_text("def test_b(): pass\n")
        (self.repo / "tests/test_c.py").write_text("def test_c(): pass\n")
        (self.repo / "src.test.js").write_text("it('x', () => {});\n")
        (self.repo / "package.json").write_text(json.dumps({"scripts": {"test": "vitest run"}}))
        (self.repo / ".github/workflows/ci.yml").write_text("on: push\n")
        (self.repo / "package-lock.json").write_text("{}\n")
        (self.repo / "__snapshots__/a.snap").write_text("snap\n")
        base = self.commit()
        self.assertEqual(module.test_changes(self.repo, base), [])
        (self.repo / "tests/test_a.py").write_text("def test_a():\n    assert 1\n")
        (self.repo / "tests/test_b.py").unlink()
        (self.repo / "tests/test_c.py").rename(self.repo / "tests/test_d.py")
        (self.repo / "src.test.js").write_text("it.skip('x', () => {});\nit.only('y', () => {});\n")
        (self.repo / "package.json").write_text(json.dumps({"scripts": {"test": "vitest run --passWithNoTests"}}))
        (self.repo / ".github/workflows/ci.yml").write_text("on: pull_request\n")
        (self.repo / "package-lock.json").write_text("{\"x\":1}\n")
        (self.repo / "__snapshots__/a.snap").unlink()
        (self.repo / "lib.py").write_text("x = 1\n")  # ordinary code is never a row
        self.commit("weaken")
        self.assertEqual(module.test_changes(self.repo, base), [
            "deleted test tests/test_b.py", "renamed test tests/test_c.py → tests/test_d.py",
            "trimmed test src.test.js (−1 lines)", "trimmed test tests/test_a.py (−1 lines)",
            "skip/only/xfail added in src.test.js (+2)", "test script changed in package.json",
            "CI changed .github/workflows/ci.yml", "snapshot removed __snapshots__/a.snap", "lockfile changed package-lock.json"])
        for n in range(30):
            (self.repo / f"tests/test_{n:02}.py").write_text("x\n")
        middle = self.commit("many")
        for n in range(30):
            (self.repo / f"tests/test_{n:02}.py").unlink()
        self.commit("delete many")
        rows = module.test_changes(self.repo, middle)
        self.assertEqual(len(rows), module.TEST_CHANGE_ROWS)
        self.assertEqual(rows[-1], "… 11 more")
        self.assertTrue(all(len(json.dumps(row)) <= module.TEST_CHANGE_ROW for row in rows))
        # A full run: the rows ride in the record, cost the checks budget, and never move the verdict.
        self.add("nightly", "--check", str(self.bin / "fakecheck"))
        self.call("worker", "nightly", env=dict(self.env, FAKE_AGENT="weaken"))
        record = self.records()[0]
        self.assertEqual(record["result"]["verdict"], "ready-for-review")
        self.assertEqual(record["result"]["test_changes"], [
            "deleted test tests/test_a.py", "skip/only/xfail added in tests/test_b.py (+1)", "lockfile changed package-lock.json"])
        self.assertEqual(record["result"]["checks"][0]["exit_code"], 0)
        self.call("check", "results")
        self.assertTrue(module.valid_result(record["result"]))
        for broken in ("rows", ["ok", 3], ["a\nb"], ["x" * 200], ["r"] * (module.TEST_CHANGE_ROWS + 1)):
            self.assertFalse(module.valid_result({**record["result"], "test_changes": broken}), broken)
        with patch.object(module, "test_changes", return_value=["r" * 100] * module.TEST_CHANGE_ROWS):
            budget = {}
            with patch.object(module, "run_check", side_effect=lambda *a: budget.update(b=a[-1]) or {"cmd": "x", "exit_code": 0, "seconds": 0, "tail": "", "source": "owner"}):
                result = module.result_package(module.read_job("nightly"), {"status": "succeeded"}, Path(record["dir"]), record["base"], Path(record["dir"]) / "log", 0)
        self.assertEqual(budget["b"], module.TAIL_BUDGET - len(json.dumps(result["test_changes"])))
        # No commits: nothing to inspect, so the key is absent and the budget whole.
        self.assertNotIn("test_changes", module.package("no-changes"))

    def test_checks_are_detected_from_the_base_commit_when_none_are_scheduled(self):
        module = self.module()
        self.assertEqual(module.detect_checks(self.repo, self.commit()), [])
        (self.repo / "package.json").write_text(json.dumps({"scripts": {"test": 'echo "Error: no test specified" && exit 1'}}))
        self.assertEqual(module.detect_checks(self.repo, self.commit()), [])
        (self.repo / "package.json").write_text(json.dumps({"scripts": {"test": "vitest run"}}))
        self.assertEqual(module.detect_checks(self.repo, self.commit()), ["vitest run"])
        (self.repo / "package.json").unlink()
        (self.repo / "pyproject.toml").write_text("[project]\nname='x'\n")
        self.assertEqual(module.detect_checks(self.repo, self.commit()), ["pytest"])
        (self.repo / "pyproject.toml").unlink()
        (self.repo / "Makefile").write_text("build:\n\ttrue\ntest:\n\ttrue\n")
        self.assertEqual(module.detect_checks(self.repo, self.commit()), ["make test"])
        (self.repo / "Makefile").write_text("build:\n\ttrue\n")
        base = self.commit()
        self.assertEqual(module.detect_checks(self.repo, base), [])
        # The checkout is not consulted: an uncommitted package.json detects nothing.
        (self.repo / "package.json").write_text(json.dumps({"scripts": {"test": "vitest run"}}))
        self.assertEqual(module.detect_checks(self.repo, base), [])

    def test_runner_runs_the_base_test_script_not_the_agents(self):
        # The base's script string runs directly with the worktree's node_modules/.bin on PATH and GRAVE_BASE set.
        base = self.node_repo()
        self.exe("codex", 'import json,subprocess\nfrom pathlib import Path\n'
                 'Path("file.txt").write_text("FAIL\\n")\n'
                 'Path("package.json").write_text(json.dumps({"scripts": {"test": "exit 0"}}))\n'
                 'subprocess.run(["git","commit","-qam","weaken"], check=True)\n')
        self.add()
        self.call("worker", "nightly")
        record = self.records()[0]
        self.assertEqual(record["base"], base)
        self.assertEqual(record["result"]["verdict"], "checks-failed")
        check = record["result"]["checks"][0]
        self.assertEqual((check["cmd"], check["exit_code"], check["source"]), ("vitest run", 1, "base:" + base))
        self.assertIn("vitest " + base, check["tail"])
        self.call("check", "results")
        # Doctor refuses a check that came from anywhere else.
        path = next((self.store / "nightly/runs").glob("*.json"))
        for source in ("base:" + "0" * 40, "head"):
            path.write_text(json.dumps({**record, "result": {**record["result"], "checks": [{**check, "source": source}]}}))
            self.assertIn("valid verdict" if source == "head" else "not the owner", self.call("check", "results", ok=False).stderr)

    def node_repo(self):
        """A base whose test script is `vitest run`; the fake vitest exits 1 when
        file.txt says FAIL and is reachable only through the worktree's node_modules/.bin."""
        (self.repo / "node_modules/.bin").mkdir(parents=True)
        tools = self.root / "tools"; tools.mkdir()
        self.assertNotIn(str(tools), self.env["PATH"])
        (tools / "vitest").write_text("#!" + sys.executable + '\nimport os,sys\nprint("vitest", os.environ["GRAVE_BASE"], flush=True)\nsys.exit(1 if "FAIL" in open("file.txt").read() else 0)\n')
        (tools / "vitest").chmod(0o755)
        (self.repo / "node_modules/.bin/vitest").symlink_to(tools / "vitest")
        (self.repo / "package.json").write_text(json.dumps({"scripts": {"test": "vitest run"}}))
        return self.commit("node")

    def test_replacing_the_base_commit_from_the_worktree_does_not_change_the_check(self):
        # refs/replace is shared through the common git dir; the runner reads the base with it disabled.
        base = self.node_repo()
        self.exe("codex", 'import json,subprocess\nfrom pathlib import Path\n'
                 'run=lambda *a: subprocess.run(["git",*a], check=True, capture_output=True, text=True).stdout.strip()\n'
                 'base=run("rev-parse","HEAD")\n'
                 'Path("file.txt").write_text("FAIL\\n")\n'
                 'Path("package.json").write_text(json.dumps({"scripts": {"test": "exit 0"}}))\n'
                 'run("add","-A")\n'
                 'run("replace","-f",base,run("commit-tree",run("write-tree"),"-m","fake base"))\n')
        self.add()
        self.call("worker", "nightly")
        self.assertIn(base, subprocess.run(["git", "-C", str(self.repo), "replace", "-l"], env=self.env, capture_output=True, text=True).stdout)
        module = self.module()
        self.assertEqual(module.detect_checks(self.repo, base), ["vitest run"])
        record = self.records()[0]
        check = record["result"]["checks"][0]
        self.assertEqual((record["result"]["verdict"], check["cmd"], check["exit_code"], check["source"]), ("checks-failed", "vitest run", 1, "base:" + base))
        self.assertEqual(record["result"]["changes"]["commits"], 0)

    def test_test_entry_point_added_by_the_agent_is_not_a_check(self):
        self.exe("codex", 'import json,subprocess\nfrom pathlib import Path\n'
                 'Path("package.json").write_text(json.dumps({"scripts": {"test": "exit 0"}}))\n'
                 'subprocess.run(["git","add","package.json"], check=True)\nsubprocess.run(["git","commit","-qm","add tests"], check=True)\n')
        self.add()
        self.call("worker", "nightly")
        result = self.records()[0]["result"]
        self.assertEqual((result["verdict"], result["checks"], result["changes"]["commits"]), ("unverified", [], 1))
        self.assertIn("No checks ran", (self.root / "notifications").read_text())

    def test_check_tails_share_one_budget_and_records_stay_under_the_doctor_limit(self):
        module = self.module()
        self.assertEqual(module.fit("", 2), "")
        self.assertEqual(module.fit("abc", 2), "")
        self.assertEqual(module.fit("abcdef", 6), "cdef")
        self.assertEqual(module.fit("abcdef", 6, head=True), "abcd")
        self.assertLessEqual(len(json.dumps(module.fit("\x1b[31m\u00e9" * 2000, 3000))), 3000)
        self.exe("noisy", 'import sys\nsys.stdout.write("\\x1b[31m\\u00e9\\ufffd" * 1000)\n')
        # Eight 500-character commands of emoji and CJK, which JSON escapes to 6 to 12 bytes each.
        long = str(self.bin / "noisy") + " # " + "\U0001f480\u6f22" * 300
        commands = [long[:500] for _ in range(8)]
        self.add("nightly", *[arg for cmd in commands for arg in ("--check", cmd)])
        self.call("worker", "nightly", env=dict(self.env, FAKE_AGENT="commit"))
        path = next((self.store / "nightly/runs").glob("*.json"))
        self.assertLess(path.stat().st_size, 48 * 1024)
        checks = self.records()[0]["result"]["checks"]
        self.assertEqual(len(checks), 8)
        self.assertLessEqual(len(json.dumps([c["cmd"] + c["tail"] for c in checks])), 24 * 1024 + 20)
        self.assertTrue(all(c["tail"].endswith("\ufffd") for c in checks))
        self.assertTrue(all(c["cmd"].startswith(str(self.bin / "noisy") + " # \U0001f480") and len(c["cmd"]) < 500 for c in checks))
        self.call("check", "results")
        # A record padded past read_private's 64 KiB cap is doctor's problem only: recovery, the
        # listing and the dashboard skip it with a warning, and the scheduler still launches the job.
        path.write_text(path.read_text()[:-2] + ', "pad": "' + "x" * (64 * 1024) + '"}\n')
        self.assertIn("exceeds", self.call("check", ok=False).stderr)
        listing = self.call("jobs")
        self.assertIn("exceeds", listing.stderr)
        self.assertIn("last —", listing.stdout)
        value = self.job(); value["next_due"] = time.time() - 1
        (self.store / "nightly/job.json").write_text(json.dumps(value))
        self.background("scheduler")
        self.wait_for(lambda: len(self.records()) == 2 and self.records()[1]["status"] == "running")
        report = load_dashboard(dict(self.env, GRAVEDECAY_ALLOWED_USERS="owner@example.test")).collect_scheduled_runs()
        self.assertIn("oversized", report["error"])
        self.assertEqual([run["status"] for run in report["runs"]], ["running"])

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

    def test_gaming_mode_defers_due_jobs_without_a_record_and_runs_after_the_thaw(self):
        self.add("nightly", "--on", "Mon 02:00")
        path = self.store / "nightly/job.json"; value = self.job(); due = value["next_due"] = time.time() - 60
        path.write_text(json.dumps(value))
        self.freeze.write_text("1\n")
        self.call("worker", "nightly")
        self.assertEqual(self.records(), [])
        self.assertEqual(self.job()["next_due"], due)
        self.assertFalse((self.root / "calls").exists())
        self.assertFalse((self.root / "worktrees").exists())
        self.call("check"); self.call("check", "results")
        self.freeze.write_text("0\n")
        self.call("worker", "nightly", env=dict(self.env, FAKE_AGENT="commit"))
        self.assertEqual(self.records()[0]["status"], "succeeded")
        self.assertEqual(self.records()[0]["due"], due)
        self.assertGreater(self.job()["next_due"], time.time())

    def test_t3_stopped_or_masked_does_not_stop_a_job(self):
        # The fake systemctl reports t3code inactive for every query; the verdict still arrives.
        self.add("nightly", "--check", "true")
        self.call("worker", "nightly", env=dict(self.env, FAKE_AGENT="commit"))
        self.assertEqual(self.records()[0]["result"]["verdict"], "ready-for-review")
        unit = (ROOT / "systemd/gravedecay-agents.service.tmpl").read_text()
        self.assertNotIn("t3code", unit)
        self.call("check")
        self.assertIn("depends on t3code.service", self.call("check", ok=False, env=dict(self.env, FAKE_AFTER="network.target t3code.service")).stderr)

    def test_entering_gaming_mode_cancels_an_active_run_and_requeues_it_once(self):
        self.add(); proc = self.background()
        self.wait_for(lambda: (self.root / "started").exists())
        self.freeze.write_text("1\n")
        proc.wait(timeout=10)
        first = self.records()[0]
        self.assertEqual((first["status"], first["reason"]), ("cancelled", "gaming mode entered during run"))
        job = self.job()
        self.assertEqual(job["requeue"], {"run_id": first["requeued"], "of": first["run_id"]})
        self.assertLessEqual(job["next_due"], time.time())
        self.assertIn("Scheduled nightly: cancelled", (self.root / "notifications").read_text())
        self.call("check"); self.call("check", "results")
        # Still gaming: the requeued run waits. Thawed: it runs under the promised id and is final.
        self.call("worker", "nightly")
        self.assertEqual(len(self.records()), 1)
        self.freeze.write_text("0\n")
        (self.root / "started").unlink()
        proc = self.background()
        self.wait_for(lambda: (self.root / "started").exists())
        self.freeze.write_text("1\n")
        proc.wait(timeout=10)
        second = next(r for r in self.records() if r["run_id"] == first["requeued"])
        self.assertEqual((second["status"], second["requeue_of"]), ("cancelled", first["run_id"]))
        self.assertNotIn("requeued", second)
        self.assertNotIn("requeue", self.job())
        self.assertEqual(len(self.records()), 2)
        self.call("check")
        # Doctor refuses a gaming cancel that nobody requeued, and a malformed requeue marker.
        path = next(p for p in (self.store / "nightly/runs").glob("*.json") if json.loads(p.read_text())["run_id"] == first["run_id"])
        path.write_text(json.dumps({k: v for k, v in first.items() if k != "requeued"}))
        self.assertIn("never requeued", self.call("check", ok=False).stderr)
        path.write_text(json.dumps(first))
        job = self.job(); job["requeue"] = {"run_id": "../x", "of": first["run_id"]}
        (self.store / "nightly/job.json").write_text(json.dumps(job))
        self.assertIn("invalid requeue marker", self.call("check", ok=False).stderr)

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
        self.assertEqual(report["runs"][0]["result"]["verdict"], "unverified")
        self.assertIn("literal output", report["runs"][0]["tail"])
        self.assertNotIn("prompt", json.dumps(report))
        self.assertNotIn("PWNED", json.dumps(report))
        dash._state = lambda headers: {"tmux": [], "agent_history": []}
        self.assertNotIn("scheduled", dash.state({}))
        self.assertIn("scheduled", dash.state({"Tailscale-User-Login": "owner@example.test"}))

    def test_diff_route_is_owner_gated_plain_text_and_capped(self):
        self.add(); self.call("worker", "nightly", env=dict(self.env, FAKE_AGENT="commit"))
        record = self.records()[0]
        dash = load_dashboard(dict(self.env, GRAVEDECAY_ALLOWED_USERS="owner@example.test"))
        server = dash.ThreadingHTTPServer(("127.0.0.1", 0), dash.Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        self.addCleanup(lambda: (server.shutdown(), server.server_close(), thread.join()))
        def get(query, owner=True):
            request = urllib.request.Request(f"http://127.0.0.1:{server.server_port}/api/run-diff?{query}",
                                             headers={"Tailscale-User-Login": "owner@example.test"} if owner else {})
            try:
                with urllib.request.urlopen(request, timeout=10) as response:
                    return response.status, dict(response.headers), response.read().decode()
            except urllib.error.HTTPError as error:
                return error.code, dict(error.headers), error.read().decode()
        code, headers, text = get("name=" + record["session"])
        self.assertEqual(code, 200, text)
        self.assertEqual(headers["Content-Type"], "text/plain; charset=utf-8")
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertIn("+finished", text)
        self.assertIn("result.txt", text)
        self.assertEqual(get("name=" + record["session"], owner=False)[0], 403)
        self.assertEqual(get("name=../" + record["session"])[0], 400)
        self.assertEqual(get("name=" + record["session"] + "x")[0], 404)
        # A non-scheduled session directory (no scheduled_job in meta.json) is refused.
        (self.root / "agents/job-manual").mkdir()
        (self.root / "agents/job-manual/meta.json").write_text(json.dumps({"dir": record["dir"], "base": record["base"]}))
        (self.root / "agents/job-manual/meta.json").chmod(0o600)
        self.assertEqual(get("name=job-manual")[0], 404)
        dash.DIFF_LIMIT = 16
        code, _, text = get("name=" + record["session"])
        self.assertEqual(code, 200)
        self.assertEqual(len(text.split("\n[diff truncated")[0]), 16)
        self.assertIn("[diff truncated at 256 KiB", text)

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
        self.assertEqual(self.records()[0]["result"], {"verdict": "unverified", "checks": [], "cost": None,
            "changes": {"commits": 1, "files": 1, "insertions": 1, "deletions": 0, "dirty": False}})
        self.call("check", "results")
        passing = {"cmd": "x", "exit_code": 0, "seconds": 1, "tail": ""}
        for broken in ({"verdict": "green"}, {**value["result"], "verdict": "succeeded"}, {**value["result"], "checks": [{"cmd": "x"}]},
                       {**value["result"], "checks": [{**passing, "source": "head"}]},
                       # Verdicts inconsistent with the checks or with a succeeded run.
                       {**value["result"], "verdict": "ready-for-review", "checks": []},
                       {**value["result"], "verdict": "ready-for-review", "checks": [passing, {**passing, "exit_code": 1}]},
                       {**value["result"], "verdict": "timed-out"}, {**value["result"], "verdict": "provider-failed"}):
            path.write_text(json.dumps({**value, "result": broken}))
            self.call("check", "results", ok=False)
            module.recover("nightly")  # malformed results fall closed, never open
            self.assertEqual(self.records()[0]["result"]["verdict"], "unverified")
        # Today's four-key record and a check without `source` stay valid; extra keys are tolerated.
        four = {"verdict": "ready-for-review", "changes": None, "checks": [{"cmd": "x", "exit_code": 0, "seconds": 1, "tail": ""}], "cost": None}
        for valid in (four, {**four, "test_changes": []}):
            path.write_text(json.dumps({**value, "result": valid}))
            self.call("check", "results")
            module.recover("nightly")
            self.assertEqual(self.records()[0]["result"], valid)
        shutil.rmtree(value["dir"])
        for status, result in (("failed", four), ("failed", None), ("cancelled", {**four, "verdict": "timed-out"})):
            path.write_text(json.dumps({**legacy, "status": status, **({"result": result} if result else {})}))
            self.assertIn("no valid verdict", self.call("check", "results", ok=False).stderr)
            module.recover("nightly")
            self.assertEqual(self.records()[0]["result"]["verdict"], "provider-failed" if status == "failed" else status)
        path.write_text(json.dumps(legacy))
        module.recover("nightly")
        # Without a worktree to inspect, nothing is known about the work.
        self.assertEqual(self.records()[0]["result"], {"verdict": "unverified", "changes": None, "checks": [], "cost": None})

    def test_codex_quota_snapshot_is_recorded_from_the_runs_own_rollout_and_checked_by_doctor(self):
        self.add("nightly", "--agent", "codex", "--check", "echo ok")
        self.call("worker", "nightly", env=dict(self.env, FAKE_AGENT="commit", FAKE_QUOTA="trailing-premium"))
        record = self.records()[0]
        self.assertEqual(record["result"]["verdict"], "ready-for-review")
        self.assertEqual(record["result"]["cost"]["model"], "gpt-5.3-codex")
        # The trailing `premium` entry has no windows; the codex entry before it is the snapshot, labelled by window length.
        self.assertEqual(record["result"]["quota"], {"provider": "codex", "account_wide": True, "plan": "prolite",
            "windows": [{"window_minutes": 10080, "used_percent": 70.0, "resets_at": 1790757683}]})
        self.assertNotIn("primary", json.dumps(record["result"]["quota"]))
        self.call("check", "results")
        self.module().recover("nightly")
        self.assertEqual(self.records()[0], record)
        path = next((self.store / "nightly/runs").glob("*.json"))
        good = record["result"]["quota"]
        for broken in ({**good, "windows": []}, {**good, "windows": [{"primary": {"used_percent": 1}}]},
                       {**good, "windows": [{"window_minutes": 0, "used_percent": 1.0, "resets_at": 1790757683}]},
                       {**good, "windows": [{"window_minutes": "weekly", "used_percent": 1.0, "resets_at": 1790757683}]},
                       {**good, "windows": [{"window_minutes": 10080, "used_percent": 1.0, "resets_at": None}]},
                       {**good, "windows": [{"window_minutes": 10080, "used_percent": 1.0, "resets_at": "tomorrow"}]},
                       {**good, "account_wide": False}, {**good, "provider": "claude"}, "46%"):
            path.write_text(json.dumps({**record, "result": {**record["result"], "quota": broken}}))
            self.assertIn("quota snapshot without a valid window_minutes and reset time", self.call("check", "results", ok=False).stderr)
        # A run without a rollout of its own carries no snapshot and the older four-key record stays valid.
        path.write_text(json.dumps({**record, "result": {k: v for k, v in record["result"].items() if k != "quota"}}))
        self.call("check", "results")

    def test_claude_run_ending_on_a_429_is_deferred_to_the_transcripts_reset_time(self):
        resets = int(time.time()) + 3600
        self.add("nightly", "--agent", "claude", "--on", "Mon 02:00")
        path = self.store / "nightly/job.json"; value = self.job(); before = value["next_due"] = time.time() - 60
        path.write_text(json.dumps(value))
        self.call("worker", "nightly", env=dict(self.env, FAKE_AGENT="ratelimit", FAKE_RESETS=str(resets)))
        record = self.records()[0]
        self.assertEqual((record["status"], record["exit_code"]), ("failed", 1))
        self.assertEqual(record["result"]["verdict"], "provider-failed")
        self.assertEqual(record["deferred_to"], resets)
        self.assertIn("Claude usage limit reached (five_hour); resets ", record["reason"])
        self.assertEqual(self.job()["next_due"], resets)  # sooner than next Monday
        self.assertIn("Claude usage limit reached", (self.root / "notifications").read_text())
        self.call("check", "results")
        # A reset already in the past, or too far out, defers nothing; a one-shot job gets its retry.
        for stale in (int(time.time()) - 60, int(time.time()) + 30 * 86400):
            self.add("once" + str(stale)[-5:], "--agent", "claude")
            self.call("worker", "once" + str(stale)[-5:], env=dict(self.env, FAKE_AGENT="ratelimit", FAKE_RESETS=str(stale)))
            self.assertNotIn("deferred_to", self.records("once" + str(stale)[-5:])[0])
            self.assertIsNone(self.job("once" + str(stale)[-5:])["next_due"])
        self.add("oneshot", "--agent", "claude")
        self.call("worker", "oneshot", env=dict(self.env, FAKE_AGENT="ratelimit", FAKE_RESETS=str(resets)))
        self.assertEqual(self.job("oneshot")["next_due"], resets)
        self.assertEqual(self.records("oneshot")[0]["deferred_to"], resets)
        # Codex is never deferred on the Claude transcript, and a plain failure without a 429 is not retried.
        self.add("codexjob", "--agent", "codex")
        self.call("worker", "codexjob", env=dict(self.env, FAKE_AGENT="ratelimit", FAKE_RESETS=str(resets)))
        self.assertIsNone(self.job("codexjob")["next_due"])
        self.assertNotIn("deferred_to", self.records("codexjob")[0])

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
        self.assertNotIn("t3code", unit)
        for path in ("bin/grave", "raise.sh", "uninstall.sh"):
            self.assertIn("gravedecay-agents.service", (ROOT / path).read_text())
        raise_sh = (ROOT / "raise.sh").read_text()
        self.assertIn("agent-jobs.py", raise_sh)
        self.assertIn('install -m 644 "$REPO_DIR/libexec/providers.py" "$GRAVE_ROOT/scripts/providers.py"', raise_sh)
        grave = (ROOT / "bin/grave").read_text()
        self.assertIn('check "scheduled run records carry a valid verdict" env GRAVE_ROOT="$GRAVE_ROOT" python3 "$GRAVE_ROOT/scripts/agent-jobs.py" check results', grave)
