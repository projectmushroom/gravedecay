import json
from pathlib import Path
import re
import subprocess
import unittest


ROOT = Path(__file__).resolve().parents[1]
GRAVE = (ROOT / "bin/grave").read_text()


class BrowserProtectionTests(unittest.TestCase):
    def test_service_image_server_and_test_client_versions_match(self):
        compose = (ROOT / "docker/browsers/compose.yaml").read_text()
        package = json.loads((ROOT / "package.json").read_text())
        lock = json.loads((ROOT / "package-lock.json").read_text())
        version = package["devDependencies"]["@playwright/test"]
        self.assertEqual(re.search(r"image: mcr.microsoft.com/playwright:v([\d.]+)-noble", compose)[1], version)
        self.assertEqual(re.search(r"npx -y playwright@([\d.]+) run-server", compose)[1], version)
        self.assertEqual(lock["packages"]["node_modules/playwright"]["version"], version)
        self.assertGreaterEqual(tuple(map(int, version.split("."))), (1, 55, 1))
        self.assertIn('"127.0.0.1:3050:3050"', compose)

    def helper(self, name, setup):
        function = re.search(rf"^{name}\(\) \{{.*?^\}}", GRAVE, re.M | re.S)[0]
        return subprocess.run(["bash", "-eu", "-c", setup + "\n" + function +
                               f"\nif {name}; then exit 0; else exit 1; fi"],
                              capture_output=True, text=True)

    def test_doctor_rejects_missing_frame_policy_and_failed_probe(self):
        good = "HTTP/1.1 200 OK\r\nContent-Security-Policy: frame-ancestors 'none'\r\nX-Frame-Options: DENY\r\n\r\n"
        import shlex
        for multiuser in (0, 1):
            for headers, rc, expected in ((good, 0, 0), (good, 1, 1),
                                           (good.replace("DENY", "SAMEORIGIN"), 0, 1),
                                           ("HTTP/1.1 200 OK\r\n\r\n" + good, 0, 1)):
                with self.subTest(multiuser=multiuser, headers=headers, rc=rc):
                    command = "dashboard_probe" if multiuser else "curl"
                    setup = f"MULTI_USER={multiuser}; DASH_PORT=4712\n{command}() {{ printf %s {shlex.quote(headers)}; return {rc}; }}"
                    self.assertEqual(self.helper("dashboard_frame_policy_ok", setup).returncode, expected)

    def test_doctor_detects_stale_browser_image_and_command_failure(self):
        for actual, rc, expected in (("new-image", 0, 0), ("old-image", 0, 1), ("new-image", 1, 1)):
            setup = f'compose() {{ echo new-image; }}; dk() {{ echo {actual}; return {rc}; }}'
            self.assertEqual(self.helper("browsers_image_ok", setup).returncode, expected)


if __name__ == "__main__":
    unittest.main()
