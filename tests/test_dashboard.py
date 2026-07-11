import importlib.util
import json
import pathlib
import threading
import unittest
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

    def test_offline_shell_contains_no_machine_state(self):
        with self.get("/offline.html") as response:
            page = response.read().decode()
        self.assertIn("Tailscale", page)
        self.assertNotIn("api/state", page)
        self.assertNotIn("BOOT", page)

    def test_compact_layout_reflows_instead_of_clipping(self):
        page = DASHBOARD.PAGE
        self.assertIn("@media(max-width:520px)", page)
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


if __name__ == "__main__":
    unittest.main()
