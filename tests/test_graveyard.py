import json
import threading
import unittest
import urllib.error
import urllib.request
from unittest.mock import patch

from test_dashboard import load_dashboard


SUMMARY = {"product": "gravedecay", "api_version": 1,
           "node": {"host": "vm", "platform": "linux", "mode": "developer"},
           "resources": {"cpu_pct": 5}, "activity": {"sessions_live": 2},
           "health": {"services_failed": 0}, "links": {"dashboard": "/grave/", "t3": "/"}}


class GraveyardTests(unittest.TestCase):
    def setUp(self):
        self.dash = load_dashboard({"GRAVEDECAY_PLATFORM": "linux", "GRAVEDECAY_BACKEND_TOKEN": "",
                                    "GRAVEDECAY_REQUIRE_BACKEND_TOKEN": "0"})

    def test_discovery_only_probes_valid_online_tailnet_candidates(self):
        good = {"Online": True, "ID": "one", "DNSName": "VM.tail.ts.net.", "HostName": "VM"}
        status = {"BackendState": "Running", "Self": good, "Peer": {
            "duplicate": good, "offline": dict(good, ID="two", Online=False),
            "invalid": dict(good, ID="three", DNSName="vm.ts.net@external.example"),
            "missing-id": dict(good, ID=None), "malformed": None}}
        self.assertEqual(self.dash.graveyard_candidates(status), [{"id": "one", "dns": "vm.tail.ts.net", "name": "VM"}])
        self.assertEqual(self.dash.graveyard_candidates(dict(status, BackendState="NeedsLogin")), [])
        self.assertEqual(self.dash.graveyard_candidates([]), [])

    def test_summary_strips_private_fields_and_rejects_unsafe_links(self):
        value = json.loads(json.dumps(SUMMARY))
        value.update(repos=["private"], token="secret")
        value["resources"].update(cpu_pct=float("nan"), memory_pct=True)
        value["links"].update(terminal="//external.example", network="/net/../term", private="/grave/")
        clean = self.dash.graveyard_summary(json.dumps(value))
        self.assertEqual(clean["links"], SUMMARY["links"])
        self.assertIsNone(clean["resources"]["cpu_pct"])
        self.assertIsNone(clean["resources"]["memory_pct"])
        self.assertNotIn("private", json.dumps(clean))
        self.assertNotIn("secret", json.dumps(clean))
        for raw in ("[]", "{}", '"text"', "x" * 65537, json.dumps(dict(SUMMARY, api_version=2))):
            self.assertIsNone(self.dash.graveyard_summary(raw))

    def test_probe_has_bounded_timeout_no_redirects_or_forwarded_credentials(self):
        with patch.object(self.dash, "sh", return_value=(0, json.dumps(SUMMARY), "")) as run:
            plot = self.dash.graveyard_probe({"id": "one", "dns": "vm.tail.ts.net", "name": "VM"})
        command = run.call_args.args[0]
        self.assertEqual(command[:2], ["curl", "-q"])
        self.assertEqual(command[-1], "https://vm.tail.ts.net/grave/api/v1/summary")
        self.assertIn("--max-filesize", command)
        self.assertIn("--max-time", command)
        self.assertIn("--noproxy", command)
        self.assertNotIn("-L", command)
        self.assertNotIn("--header", command)
        self.assertEqual(plot["summary"]["activity"]["sessions_live"], 2)

    def test_collector_is_single_flight_and_does_not_block_dashboard(self):
        started, finish = threading.Event(), threading.Event()
        def scan():
            started.set()
            finish.wait(2)
        with patch.object(self.dash, "_scan_graveyard", side_effect=scan) as worker:
            try:
                self.assertEqual(self.dash.graveyard()["state"], "scanning")
                self.assertTrue(started.wait(1))
                self.assertEqual(self.dash.graveyard()["state"], "scanning")
                self.assertEqual(worker.call_count, 1)
            finally:
                finish.set()

    def test_inventory_is_owner_only_and_unsupported_platforms_do_not_scan(self):
        self.dash.ALLOWED_USERS = {"owner@example.test"}
        server = self.dash.ThreadingHTTPServer(("127.0.0.1", 0), self.dash.Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            url = f"http://127.0.0.1:{server.server_port}/grave/api/graveyard"
            with patch.object(self.dash, "graveyard", return_value={"state": "ready", "plots": []}) as collect:
                for headers in ({}, {"Tailscale-User-Login": "viewer@example.test"}):
                    with self.assertRaises(urllib.error.HTTPError) as denied:
                        urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=2)
                    self.assertEqual(denied.exception.code, 403)
                collect.assert_not_called()
                with urllib.request.urlopen(urllib.request.Request(url, headers={"Tailscale-User-Login": "owner@example.test"}), timeout=2) as response:
                    self.assertEqual(json.load(response)["state"], "ready")
                    self.assertEqual(response.headers["Cache-Control"], "no-store")
                    self.assertIsNone(response.headers.get("Access-Control-Allow-Origin"))
        finally:
            server.shutdown(); server.server_close(); thread.join(timeout=2)
        for flag in ("PORTABLE", "BACKEND_TOKEN", "REQUIRE_BACKEND_TOKEN"):
            with patch.object(self.dash, flag, True), patch.object(self.dash, "_scan_graveyard") as scan:
                self.assertEqual(self.dash.graveyard()["state"], "unsupported")
                scan.assert_not_called()
