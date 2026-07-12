import importlib.util
import hashlib
import json
import pathlib
import threading
import unittest
import urllib.error
import urllib.request


ROOT = pathlib.Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("gravedecay_dashboard", ROOT / "dashboard/gravedecay.py")
DASHBOARD = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(DASHBOARD)


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
