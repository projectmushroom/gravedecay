import json
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import mock_open, patch

from test_dashboard import load_dashboard
from test_graveyard import SUMMARY

ROOT = Path(__file__).resolve().parents[1]


class OSIdentityTests(unittest.TestCase):
    def setUp(self):
        self.dash = load_dashboard({"GRAVEDECAY_PLATFORM": "linux"})

    def test_distribution_and_family_detection_never_executes_release_file(self):
        for release, icon, name in (
            ('ID=omarchy\nID_LIKE=arch\nPRETTY_NAME="Omarchy"', 'archlinux', 'Omarchy'),
            ('ID=unknown-arch\nID_LIKE="arch linux"', 'archlinux', 'Linux'),
            ('ID=ubuntu\nID_LIKE=debian\nNAME=Ubuntu', 'ubuntu', 'Ubuntu'),
            ('ID=opensuse-tumbleweed\nNAME="openSUSE Tumbleweed"', 'opensuse', 'openSUSE Tumbleweed'),
            ('ID=rocky\nID_LIKE="rhel centos fedora"', 'redhat', 'Linux'),
            ('ID=unknown\nPRETTY_NAME="$(touch /tmp/no-exec)"', 'tux', '$(touch /tmp/no-exec)'),
            ('ID="broken\nNAME=Linux', 'tux', 'Linux'),
        ):
            with self.subTest(release=release), patch('builtins.open', mock_open(read_data=release)):
                self.assertEqual(self.dash.os_identity(), {"os_icon": icon, "os_name": name})

    def test_missing_release_and_non_linux_platforms_have_honest_fallbacks(self):
        with patch('builtins.open', side_effect=FileNotFoundError):
            self.assertEqual(self.dash.os_identity(), {"os_icon": "tux", "os_name": "Linux"})
        for flag, icon in (("MACOS", "apple"), ("PORTABLE", "docker")):
            with patch.object(self.dash, flag, True), patch('builtins.open') as read:
                self.assertEqual(self.dash.os_identity()["os_icon"], icon)
                read.assert_not_called()

    def test_identity_survives_summary_and_state_for_every_viewer(self):
        identity = {"os_icon": "archlinux", "os_name": "Omarchy"}
        with patch.object(self.dash, "OS_IDENTITY", identity), patch.object(self.dash, "_summary", return_value=json.loads(json.dumps(SUMMARY))):
            self.assertEqual(self.dash.summary()["node"]["os_icon"], "archlinux")
            self.assertEqual(self.dash.graveyard_summary(json.dumps(self.dash.summary()))["node"]["os_name"], "Omarchy")
        with patch.object(self.dash, "OS_IDENTITY", identity), patch.object(self.dash, "_state", return_value={}), patch.object(self.dash, "owner_request", return_value=False):
            self.assertEqual(self.dash.state({}), identity)

    def test_peers_cannot_supply_asset_paths_or_unbounded_names(self):
        for value in (None, [], {}, "../../secret", "<svg onload=alert(1)>", "https://elsewhere/logo.svg"):
            node = {**SUMMARY["node"], "os_icon": value, "os_name": "x" * 500}
            clean = self.dash.graveyard_summary(json.dumps({**SUMMARY, "node": node}))
            self.assertNotIn("os_icon", clean["node"])
            self.assertEqual(len(clean["node"]["os_name"]), 256)
        self.assertIsNotNone(self.dash.graveyard_summary(json.dumps(SUMMARY)))

    def test_generated_artwork_is_current_and_embedded_in_served_page(self):
        subprocess.run([sys.executable, str(ROOT / "scripts/build-os-logos.py"), "--check"], check=True)
        self.assertNotIn("@OS_LOGOS@", self.dash.PAGE)
        for icon in self.dash.OS_ICONS:
            self.assertIn(f'id="os-{icon}"', self.dash.PAGE)


if __name__ == "__main__":
    unittest.main()
