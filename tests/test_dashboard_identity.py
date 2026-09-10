"""Real HTTP regressions for the headerless/tagged-device boundary (#149)."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

from test_dashboard import load_dashboard, ROOT


class DashboardIdentityTests(unittest.TestCase):
    def test_identity_matrix_on_every_platform_and_linux_mode(self):
        for platform, active in (("linux", "active"), ("linux", "inactive"),
                                 ("macos", "active"), ("container", "active")):
            with self.subTest(platform=platform, active=active), tempfile.TemporaryDirectory() as tmp:
                dash = load_dashboard({"GRAVE_ROOT": tmp, "GRAVEDECAY_PLATFORM": platform,
                    "GRAVEDECAY_MACOS_AGENTS": "1", "GRAVEDECAY_ALLOWED_USERS": "owner@example.test"})
                token = dash.local_token(create=True)
                real_sh = dash.sh
                dash.sh = lambda cmd, timeout=10: ((0, '{"state":"idle"}', "")
                    if cmd[-1] == "update-status" else real_sh(cmd, timeout=timeout))
                dash.unit_state = lambda _: {"active": active}
                for name in ("collect_services", "collect_repos", "collect_journal", "collect_inbox", "collect_agent_history"):
                    setattr(dash, name, lambda: [])
                dash.collect_system = lambda: {}
                dash.collect_docker = lambda: {"containers": []}
                dash.collect_backups = lambda: {"count": 1, "latest": "private-backup"}
                dash.collect_tmux = lambda: [{"name": "private-session"}]
                dash.collect_github = lambda: {"login": "private-login", "prs": []}
                dash.collect_linear = lambda: {"issues": [{"title": "private-issue"}]}
                dash.collect_ci = lambda: {"rows": []}
                dash.collect_agent_usage = lambda: None
                dash.t3_connect_state = lambda: {}
                dash.collect_macos_repo_inventory = lambda: {"repos": [], "root": tmp}
                dash.collect_macos_work = lambda _: {"github": dash.collect_github(), "ci": dash.collect_ci()}
                server = dash.ThreadingHTTPServer(("127.0.0.1", 0), dash.Handler)
                thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
                origin = f"http://127.0.0.1:{server.server_port}"
                def request(path, headers, data=None):
                    req = urllib.request.Request(origin + path, headers=headers,
                        data=None if data is None else json.dumps(data).encode())
                    try:
                        with urllib.request.urlopen(req, timeout=3) as response:
                            return response.status, json.load(response)
                    except urllib.error.HTTPError as error:
                        return error.code, json.load(error)
                try:
                    denied = ({}, {"Tailscale-User-Login": ""},
                        {"Tailscale-User-Login": "other@example.test"},
                        {"X-Grave-Local-Token": "0" * 64},
                        {"X-Forwarded-For": "127.0.0.1", "Tailscale-User-Id": "owner",
                         "X-Grave-Backend-Token": "forged", "X-Grave-Workspace": "owner"})
                    for headers in denied:
                        self.assertEqual(request("/api/auth-check", headers)[0], 403)
                        self.assertEqual(request("/api/settings", headers, {})[0], 403)
                        self.assertEqual(request("/api/action-stream?action=t3-pair", headers)[0], 403)
                        code, state = request("/api/state", headers)
                        self.assertEqual(code, 200)
                        self.assertNotIn("private-", json.dumps(state))
                        self.assertNotIn("dispatch", state)
                    for headers in ({"Tailscale-User-Login": "owner@example.test"},
                                    {"X-Grave-Local-Token": token}):
                        self.assertEqual(request("/api/auth-check", headers), (200, {"ok": True}))
                        self.assertEqual(request("/api/settings", headers, {})[0], 200)
                        state = request("/api/state", headers)[1]
                        self.assertIn("private-session", json.dumps(state))
                        self.assertNotIn(token, json.dumps(state))
                        self.assertEqual(request("/api/settings", {**headers, "Sec-Fetch-Site": "cross-site"}, {})[0], 403)
                    # CLI proves the explicit maintenance protocol against the real server.
                    env = dict(os.environ, GRAVE_ROOT=tmp, GRAVEDECAY_PORT=str(server.server_port),
                               GRAVEDECAY_PLATFORM=platform)
                    checked = subprocess.run([sys.executable, str(ROOT / "dashboard/gravedecay.py"), "--check-auth"],
                                             env=env, capture_output=True, text=True)
                    self.assertEqual(checked.returncode, 0, checked.stderr)
                finally:
                    server.shutdown(); server.server_close(); thread.join()

    def test_capability_persistence_privacy_rotation_and_empty_allowlist(self):
        with tempfile.TemporaryDirectory() as tmp:
            dash = load_dashboard({"GRAVE_ROOT": tmp, "GRAVEDECAY_ALLOWED_USERS": ""})
            self.assertFalse(dash.owner_request({}))
            self.assertFalse(dash.owner_request({"Tailscale-User-Login": "owner@example.test"}))
            token = dash.local_token(create=True)
            path = Path(dash.LOCAL_TOKEN_PATH)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(dash.local_token(create=True), token)
            self.assertTrue(dash.owner_request({"X-Grave-Local-Token": token}))
            path.chmod(0o644)
            self.assertFalse(dash.owner_request({"X-Grave-Local-Token": token}))
            path.chmod(0o600)
            target = path.with_name("saved-token"); path.rename(target); path.symlink_to(target)
            self.assertFalse(dash.owner_request({"X-Grave-Local-Token": token}))
            path.unlink()
            self.assertNotEqual(dash.local_token(create=True), token)
            self.assertFalse(dash.owner_request({"X-Grave-Local-Token": token}))
            self.assertIsNone(dash._safe_path("config/secrets/dashboard-local-token"))

    def test_portable_edge_strips_local_capability_and_doctors_enforce_boundary(self):
        nginx = (ROOT / "docker/portable/nginx.conf").read_text()
        self.assertIn('proxy_set_header X-Grave-Local-Token "";', nginx)
        for path in ("bin/grave", "macos/status.sh", "docker/portable/Dockerfile"):
            self.assertIn("--check-auth", (ROOT / path).read_text())
