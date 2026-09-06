import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import urllib.error
import urllib.request

from test_dashboard import load_dashboard

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("benchmark", ROOT / "dashboard/benchmark.py")
BENCH = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(BENCH)


class BenchmarkTests(unittest.TestCase):
    def test_real_quick_run_scores_valid_work_and_cleans_up(self):
        with tempfile.TemporaryDirectory() as root:
            p = subprocess.run([sys.executable, str(ROOT / "dashboard/benchmark.py"),
                                "--root", root, "quick", "--json"], capture_output=True,
                               text=True, timeout=90)
            self.assertEqual(p.returncode, 0, p.stderr)
            state = json.loads(p.stdout)
            r = state["latest"]
            self.assertEqual(state["state"], "completed")
            self.assertEqual(r["suite"], "grave-dev-v1")
            self.assertEqual(r["score"], round(1000 / r["single"]["total_s"]))
            self.assertEqual(len(r["samples"]), 3)
            self.assertTrue(all(s["tests_s"] > 0 for s in r["samples"]))
            store = Path(root) / "config/benchmark"
            self.assertEqual(list(store.glob("work-*")), [])
            self.assertEqual(len(list((store / "results").glob("*.json"))), 1)

    def test_duplicate_start_cancel_and_stale_state_preserve_result(self):
        with tempfile.TemporaryDirectory() as root:
            store = Path(root) / "config/benchmark"
            store.mkdir(parents=True)
            previous = {"score": 42}
            BENCH.write(store / "latest.json", previous)
            subprocess.run([sys.executable, str(ROOT / "dashboard/benchmark.py"),
                            "--root", root, "start", "--mode", "sustained"],
                           check=True, capture_output=True, timeout=5)
            try:
                with self.assertRaisesRegex(ValueError, "already running"):
                    BENCH.request(store, "start")
                self.assertEqual(BENCH.request(store, "cancel")["state"], "cancelling")
                deadline = time.monotonic() + 10
                while BENCH.request(store, "status")["state"] == "running" and time.monotonic() < deadline:
                    time.sleep(0.05)
                state = BENCH.request(store, "status")
                self.assertEqual(state["state"], "cancelled", state)
                self.assertEqual(state["latest"], previous)
                self.assertEqual(list(store.glob("work-*")), [])
                BENCH.write(store / "status.json", {"state": "running"})
                self.assertEqual(BENCH.request(store, "status")["state"], "interrupted")
            finally:
                BENCH.request(store, "cancel")

    def test_failed_workload_cannot_replace_success(self):
        with tempfile.TemporaryDirectory() as root:
            store = Path(root)
            BENCH.write(store / "latest.json", {"score": 42})
            BENCH.write(store / "status.json", {"state": "running"})
            with patch.object(BENCH.shutil, "which", return_value=None):
                BENCH.run(store, "quick")
            self.assertEqual(BENCH.status(store)["state"], "failed")
            self.assertEqual(BENCH.read(store / "latest.json"), {"score": 42})

    def test_command_timeout_kills_child(self):
        with tempfile.TemporaryDirectory() as root:
            with self.assertRaisesRegex(RuntimeError, "timed out"):
                BENCH.command([sys.executable, "-c", "import time; time.sleep(60)"],
                              root, Path(root), time.monotonic() + 0.1, os.environ.copy())

    def test_sustained_run_finishes_and_reports_later_slowdown(self):
        with tempfile.TemporaryDirectory() as root:
            store = Path(root)
            BENCH.write(store / "status.json", {"state": "running"})
            clock, cycles = [0], []

            def workload(*args):
                seconds = 10 if len(cycles) < 10 else 20
                clock[0] += seconds
                cycles.append(seconds)
                return {"total_s": seconds, "repository_s": 1, "build_s": 2, "tests_s": seconds - 3}

            with patch.object(BENCH.time, "monotonic", side_effect=lambda: clock[0]), \
                    patch.object(BENCH, "cycle", side_effect=workload), \
                    patch.object(BENCH, "command", return_value=0):
                BENCH.run(store, "sustained")
            result = BENCH.read(store / "latest.json")
            self.assertEqual(BENCH.read(store / "status.json")["state"], "completed")
            self.assertGreaterEqual(sum(cycles[4:]), 600)
            self.assertEqual(result["sustained"]["last_vs_first"], 2)
            self.assertEqual(result["sustained"]["cycles"], len(cycles) - 4)

    def test_api_permissions_validation_and_macos_support(self):
        for platform in ("linux", "macos", "container"):
            with self.subTest(platform=platform), tempfile.TemporaryDirectory() as root:
                dash = load_dashboard({"GRAVE_ROOT": root, "GRAVEDECAY_PLATFORM": platform,
                                       "GRAVEDECAY_ALLOWED_USERS": "owner@test"})
                server = dash.ThreadingHTTPServer(("127.0.0.1", 0), dash.Handler)
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                url = f"http://127.0.0.1:{server.server_port}/grave/api/admin/benchmark"

                def call(body=None, viewer="owner@test", site="same-origin"):
                    req = urllib.request.Request(url, data=None if body is None else json.dumps(body).encode(),
                                                 headers={"Tailscale-User-Login": viewer, "Sec-Fetch-Site": site,
                                                          "Content-Type": "application/json"})
                    try:
                        with urllib.request.urlopen(req, timeout=5) as r:
                            return r.status, json.load(r)
                    except urllib.error.HTTPError as e:
                        e.close()
                        return e.code, {}
                try:
                    with patch.object(dash, "benchmark", return_value=(200, {"state": "idle"})) as runner:
                        self.assertEqual(call()[0], 404 if platform == "container" else 200)
                        self.assertEqual(call(viewer="stranger@test")[0], 404 if platform == "container" else 403)
                        self.assertEqual(call({"action": "start", "mode": "quick"}, site="cross-site")[0], 403)
                        self.assertEqual(call({"action": "start", "mode": "quick;reboot"})[0], 404 if platform == "container" else 400)
                        self.assertEqual(call({"action": "start", "mode": "quick", "root": "/tmp"})[0], 404 if platform == "container" else 400)
                        self.assertEqual(call({"action": "start", "mode": "capacity"})[0], 404 if platform == "container" else 200)
                        if platform == "container":
                            runner.assert_not_called()
                        else:
                            runner.assert_called_with("start", "capacity")
                finally:
                    server.shutdown(); server.server_close(); thread.join(timeout=2)

    def test_installer_and_doctor_contract(self):
        for name in ("raise.sh", "macos/install.sh", "macos/status.sh", "bin/grave", "macos/grave"):
            self.assertIn("benchmark.py", (ROOT / name).read_text())
        self.assertIn('check "development benchmark ready"', (ROOT / "bin/grave").read_text())


if __name__ == "__main__":
    unittest.main()
