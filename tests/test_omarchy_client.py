import json
import pathlib
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
CLIENT = ROOT / "clients" / "omarchy"


class OmarchyClientContractTests(unittest.TestCase):
    def test_manifest_and_local_installer_contract(self):
        manifest = json.loads((CLIENT / "manifest.json").read_text())
        self.assertEqual(manifest["id"], "projectmushroom.gravedecay")
        self.assertEqual(manifest["kinds"], ["bar-widget"])
        self.assertEqual(manifest["barWidget"]["defaultSection"], "right")
        self.assertFalse(manifest["barWidget"]["allowMultiple"])
        self.assertIn("refreshIntervalSec", str(manifest))
        installer = (CLIENT / "install-local.sh").read_text()
        self.assertIn("omarchy plugin validate", installer)
        self.assertIn("omarchy-shell shell rescanPlugins", installer)
        self.assertIn("omarchy plugin enable", installer)
        self.assertIn("refusing to overwrite", installer)

    def test_client_uses_argv_discovery_and_versioned_summary(self):
        panel = (CLIENT / "Panel.qml").read_text()
        self.assertIn('["tailscale", "status", "--json"]', panel)
        self.assertIn('["curl", "--silent"', panel)
        self.assertIn('/grave/api/v1/summary', panel)
        self.assertNotIn('bash -c', panel)


if __name__ == "__main__":
    unittest.main()
