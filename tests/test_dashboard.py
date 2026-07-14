import importlib.util
import hashlib
import json
import os
import pathlib
import threading
import unittest
import urllib.error
import urllib.request


ROOT = pathlib.Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("gravedecay_dashboard", ROOT / "dashboard/gravedecay.py")
DASHBOARD = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(DASHBOARD)


def load_dashboard(env):
    """Load a fresh gravedecay module instance under a patched environment.

    Module-level config (GRAVE_ROOT, TMUX_SOCKET, T3_BASE_DIR, …) is read from
    os.environ at import, so a workspace instance is simulated by loading with
    that workspace's env. Importing is side-effect-free (the server starts only
    under __main__)."""
    old = dict(os.environ)
    os.environ.update(env)
    try:
        spec = importlib.util.spec_from_file_location("gravedecay_probe", ROOT / "dashboard/gravedecay.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        os.environ.clear()
        os.environ.update(old)


class DashboardContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = DASHBOARD.ThreadingHTTPServer(("127.0.0.1", 0), DASHBOARD.Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.origin = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)

    def get(self, path):
        return urllib.request.urlopen(self.origin + path, timeout=2)

    def post(self, path, data):
        return urllib.request.urlopen(urllib.request.Request(
            self.origin + path,
            data=json.dumps(data).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        ), timeout=2)

    def test_manifest_owns_the_appliance_origin(self):
        manifest = json.loads(DASHBOARD.MANIFEST)
        self.assertEqual(manifest["scope"], "/")
        self.assertEqual(manifest["display"], "standalone")
        self.assertTrue(manifest["id"].endswith("/grave/"))
        self.assertEqual(manifest["start_url"], "./")

    def test_service_worker_can_control_the_root_but_not_cache_api_data(self):
        with self.get("/sw.js") as response:
            worker = response.read().decode()
            self.assertEqual(response.headers["Service-Worker-Allowed"], "/")
            self.assertEqual(response.headers["Cache-Control"], "no-cache")
        self.assertIn("request.mode !== 'navigate'", worker)
        self.assertNotIn("/api/", worker)
        with self.get("/healthz") as response:
            self.assertEqual(response.headers["Cache-Control"], "no-store")
            health = json.loads(response.read())
        self.assertEqual(health["build"], hashlib.sha256(
            (ROOT / "dashboard/gravedecay.py").read_bytes()).hexdigest())

    def test_raise_restarts_services_after_installing_their_files(self):
        ritual = (ROOT / "raise.sh").read_text()
        self.assertIn("enable_restart gravedecay", ritual)
        self.assertIn("enable_restart gravedecay-term", ritual)
        self.assertIn("enable_restart t3code", ritual)
        self.assertNotIn("systemctl enable --now gravedecay\n", ritual)

    def test_raise_reads_multi_user_from_conf_before_gating_on_it(self):
        # Regression for the keystone multi-user bug: raise.sh gates the gateway
        # + workspace-unit install on ${MULTI_USER}, which lives ONLY in
        # grave.conf. sudo scrubs the environment, so raise.sh must read the
        # value back from the installed conf — otherwise `grave multiuser enable`
        # silently leaves the box on single-user ports with no gateway.
        ritual = (ROOT / "raise.sh").read_text()
        read = ritual.find(". /etc/gravedecay/grave.conf")
        self.assertNotEqual(read, -1, "raise.sh never sources grave.conf to load MULTI_USER")
        self.assertIn('MULTI_USER=$(. /etc/gravedecay/grave.conf', ritual)
        gate = ritual.find('if [[ "${MULTI_USER:-0}" == 1 ]]')
        self.assertNotEqual(gate, -1)
        self.assertLess(read, gate, "MULTI_USER must be read from the conf before it gates the install")

    def test_raise_does_not_blanket_chown_multi_user_subtrees(self):
        # Regression #41: a recursive chown of the whole GRAVE_ROOT steals the
        # ownership of workspaces/<slug> (grave-<slug>) and the root-owned
        # config/workspace-services/*.env on every re-raise, crash-looping the
        # @-units. The claim must prune those subtrees.
        ritual = (ROOT / "raise.sh").read_text()
        self.assertNotIn('chown -R "$RUN_USER:$RUN_USER" "$GRAVE_ROOT"', ritual)
        self.assertIn('-path "$GRAVE_ROOT/workspaces"', ritual)
        self.assertIn('-path "$GRAVE_ROOT/config/workspace-services"', ritual)

    def test_raise_health_probes_cannot_abort_the_ritual(self):
        # Regression #51: bare `curl … && ok` probes race the socket bind and,
        # under `set -e`, abort the whole ritual. All probes go through wait_http.
        ritual = (ROOT / "raise.sh").read_text()
        self.assertIn("wait_http()", ritual)
        for url in ("http://127.0.0.1:$DASH_PORT/healthz",
                    "http://127.0.0.1:$TERM_PORT/",
                    "http://127.0.0.1:$T3_PORT/",
                    "http://127.0.0.1:${GATEWAY_PORT:-4710}/healthz"):
            self.assertNotIn(f'curl -sf -o /dev/null "{url}" && ok', ritual)

    def test_multi_user_serve_and_gateway_ordering(self):
        ritual = (ROOT / "raise.sh").read_text()
        # Regression #42: stale single-user /grave and /term mounts bypass the
        # identity gateway; the multi-user branch must remove them.
        self.assertIn("--set-path=/grave off", ritual)
        self.assertIn("--set-path=/term off", ritual)
        # Regression #51: the gateway reads workspaces.json, created by
        # `__users reapply`, so it must start AFTER reapply.
        reapply = ritual.find("__users reapply")
        gateway_start = ritual.find("enable_restart gravedecay-gateway")
        self.assertNotEqual(reapply, -1)
        self.assertNotEqual(gateway_start, -1)
        self.assertLess(reapply, gateway_start)
        # …and tolerate the registry not existing yet ('-' prefix).
        unit = (ROOT / "systemd/gravedecay-gateway.service.tmpl").read_text()
        self.assertIn("-@GRAVE_ROOT@/config/workspaces.json", unit)

    def test_workspace_dashboard_targets_its_own_socket_and_pairing_dir(self):
        # Regression #43/#44: a workspace dashboard hardcoded the owner's tmux
        # socket ("agents") and t3 base-dir (agents/t3code), so the sessions
        # panel/kill and the pairing-token button acted on paths the workspace's
        # own T3/terminal never use. Both must come from the environment.
        saved = {k: os.environ.pop(k, None) for k in ("TMUX_SOCKET", "GRAVEDECAY_T3_BASE_DIR")}
        try:
            single = load_dashboard({"GRAVE_ROOT": "/srv/dev"})
        finally:
            for key, value in saved.items():
                if value is not None:
                    os.environ[key] = value
        self.assertEqual(single.TMUX_SOCKET, "agents")
        self.assertIn("/srv/dev/agents/t3code", single.ACTIONS["t3-pair"])

        ws = load_dashboard({"GRAVE_ROOT": "/srv/dev/workspaces/alice",
                             "TMUX_SOCKET": "grave-alice",
                             "GRAVEDECAY_T3_BASE_DIR": "/srv/dev/workspaces/alice/state/t3"})
        self.assertEqual(ws.TMUX_SOCKET, "grave-alice")
        self.assertIn("/srv/dev/workspaces/alice/state/t3", ws.ACTIONS["t3-pair"])
        self.assertNotIn("/srv/dev/workspaces/alice/agents/t3code", ws.ACTIONS["t3-pair"])

    def test_workspace_dashboard_unit_wires_socket_dir_and_shares_tmp(self):
        dash = (ROOT / "systemd/gravedecay-dashboard@.service.tmpl").read_text()
        term = (ROOT / "systemd/gravedecay-term@.service.tmpl").read_text()
        # The dashboard's socket must equal the terminal's, and both are grave-%i.
        self.assertIn("Environment=TMUX_SOCKET=grave-%i", dash)
        self.assertIn("Environment=TMUX_SOCKET=grave-%i", term)
        self.assertIn("Environment=GRAVEDECAY_T3_BASE_DIR=@GRAVE_ROOT@/workspaces/%i/state/t3", dash)
        # A private /tmp would hide the terminal's socket from the dashboard.
        self.assertNotIn("PrivateTmp=yes", dash)

    def test_custom_tile_urls_reject_dangerous_schemes(self):
        # Regression #60: tiles render as <a href> and iframe src, so a
        # javascript:/data: URL would be stored XSS in the dashboard origin.
        self.assertEqual(DASHBOARD._safe_tile_url("javascript:fetch('/x')"), "")
        self.assertEqual(DASHBOARD._safe_tile_url("data:text/html,x"), "")
        self.assertEqual(DASHBOARD._safe_tile_url("https://ok.test/a"), "https://ok.test/a")
        self.assertEqual(DASHBOARD._safe_tile_url("/term"), "/term")
        kept = DASHBOARD._sanitize_custom_apps([
            {"name": "evil", "url": "javascript:alert(1)"},
            {"name": "ok", "url": "https://ok.test"},
            {"name": "internal", "url": "/grave/"},
        ])
        self.assertEqual([a["url"] for a in kept], ["https://ok.test", "/grave/"])

    def test_admin_releases_requires_authorization(self):
        # Regression #47: a read-only tailnet viewer (login not in ALLOWED_USERS)
        # must not be able to run `grave releases` via this endpoint.
        request = urllib.request.Request(
            self.origin + "/api/admin/releases",
            headers={"Tailscale-User-Login": "eve@example.com"})
        with self.assertRaises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(request, timeout=2)
        self.assertEqual(error.exception.code, 403)

    def test_state_withholds_owner_private_data_from_read_only_viewers(self):
        # Regression #47: /api/state leaked open PRs, Linear backlog, agent spend,
        # repo commit subjects, and journal errors to any tailnet viewer. Force
        # developer mode (t3code inactive in CI would take the gaming branch) and
        # stub the collectors so the assertion is deterministic.
        names = ("unit_state", "collect_github", "collect_ci", "collect_linear",
                 "collect_agent_usage", "collect_repos", "collect_journal",
                 "collect_services", "collect_docker", "collect_system",
                 "collect_backups", "collect_tmux")
        saved = {n: getattr(DASHBOARD, n) for n in names}
        DASHBOARD.unit_state = lambda u: {"active": "active"}
        DASHBOARD.collect_github = lambda: {"login": "owner", "prs": [{"title": "secret"}], "error": None}
        DASHBOARD.collect_ci = lambda: {"rows": [{"repo": "x"}]}
        DASHBOARD.collect_linear = lambda: {"configured": True, "issues": [{"title": "secret"}], "error": None}
        DASHBOARD.collect_agent_usage = lambda: {"cost": 42}
        DASHBOARD.collect_repos = lambda: [{"name": "secret-repo"}]
        DASHBOARD.collect_journal = lambda: ["secret error line"]
        DASHBOARD.collect_services = lambda: []
        DASHBOARD.collect_docker = lambda: {"containers": []}
        DASHBOARD.collect_system = lambda: {}
        DASHBOARD.collect_backups = lambda: {"count": 0, "latest": None}
        DASHBOARD.collect_tmux = lambda: []
        try:
            owner = DASHBOARD.state({})  # localhost / no header → trusted
            viewer = DASHBOARD.state({"Tailscale-User-Login": "eve@example.com"})
        finally:
            for name, fn in saved.items():
                setattr(DASHBOARD, name, fn)
        self.assertEqual(owner["github"]["prs"], [{"title": "secret"}])
        self.assertEqual(owner["repos"], [{"name": "secret-repo"}])
        self.assertEqual(viewer["github"]["error"], "restricted")
        self.assertEqual(viewer["repos"], [])
        self.assertEqual(viewer["journal"], [])
        self.assertEqual(viewer["linear"]["issues"], [])
        self.assertIsNone(viewer["usage"])

    def test_offline_shell_contains_no_machine_state(self):
        with self.get("/offline.html") as response:
            page = response.read().decode()
        self.assertIn("Tailscale", page)
        self.assertNotIn("api/state", page)
        self.assertNotIn("BOOT", page)

    def test_compact_layout_reflows_instead_of_clipping(self):
        page = DASHBOARD.PAGE
        self.assertIn("@media(max-width:520px)", page)
        self.assertIn("@media(max-width:500px)", page)
        self.assertIn(".panel table,.panel tbody,.panel tr,.panel td{display:block", page)
        self.assertIn("table,tbody,tr,td{display:block", page)
        self.assertIn("height:100dvh", page)
        self.assertNotIn("html{-webkit-text-size-adjust:100%;overflow-x:hidden}", page)
        self.assertIn("id=\"connection\"", page)

    def test_self_upgrade_is_detached_from_the_dashboard_service(self):
        command = DASHBOARD.ACTIONS["update-grave"]
        self.assertIn("--no-block", command)
        self.assertEqual(command[-1], "gravedecay-upgrade.service")
        self.assertIn('data-act="update-grave"', DASHBOARD.PAGE)
        unit = (ROOT / "systemd/gravedecay-upgrade.service.tmpl").read_text()
        self.assertIn("Type=oneshot", unit)
        self.assertIn("ExecStart=@GRAVE_BIN@ upgrade", unit)
        selected = (ROOT / "systemd/gravedecay-upgrade@.service.tmpl").read_text()
        self.assertIn("ExecStart=@GRAVE_BIN@ upgrade --tag %i", selected)
        self.assertIn('id="grave-release"', DASHBOARD.PAGE)
        self.assertIn('id="install-grave-release"', DASHBOARD.PAGE)
        self.assertIn('p == "/api/admin/releases"', pathlib.Path(
            ROOT / "dashboard/gravedecay.py").read_text())

    def test_release_upgrade_validates_and_queues_a_detached_instance(self):
        calls = []
        original = DASHBOARD.sh
        DASHBOARD.sh = lambda cmd, timeout=10: (calls.append(cmd) or (0, "", ""))
        try:
            with self.assertRaises(urllib.error.HTTPError) as error:
                self.post("/api/admin/upgrade", {"tag": "v0.5.0;reboot"})
            self.assertEqual(error.exception.code, 400)
            self.assertEqual(calls, [])
            with self.post("/api/admin/upgrade", {"tag": "v0.5.0"}) as response:
                payload = json.loads(response.read())
            self.assertTrue(payload["ok"])
        finally:
            DASHBOARD.sh = original
        self.assertEqual(calls, [["sudo", "-n", "systemctl", "--no-block", "start",
                                  "gravedecay-upgrade@v0.5.0.service"]])


if __name__ == "__main__":
    unittest.main()
