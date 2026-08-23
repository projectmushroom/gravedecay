import json
import pathlib
import os
import subprocess
import tempfile
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
        self.assertIn('omarchy plugin enable "$id"', installer)
        self.assertNotIn('omarchy plugin enable "$id" --yes', installer)
        self.assertIn("jq -r '.id // empty'", installer)
        self.assertNotIn("python3", installer)
        self.assertIn("refusing to overwrite", installer)
        self.assertIn(".backup.", installer)
        self.assertNotIn(".previous", installer)
        self.assertIn("attempt < 40", installer)
        self.assertIn("omarchy plugin list --json", installer)

    def test_client_uses_argv_discovery_and_versioned_summary(self):
        panel = (CLIENT / "Panel.qml").read_text()
        self.assertIn('["tailscale", "status", "--json"]', panel)
        self.assertIn('["curl", "--silent"', panel)
        self.assertIn('/grave/api/v1/summary', panel)
        self.assertIn('"--max-filesize", "65536"', panel)
        self.assertIn("StdioCollector", panel)
        self.assertNotIn("SplitParser", panel)
        self.assertNotIn('bash -c', panel)

    def test_qml_lifecycle_and_controls_use_panel_contract(self):
        panel = (CLIENT / "Panel.qml").read_text()
        self.assertNotIn("property var settings", panel)
        self.assertNotIn("function open(", panel)
        self.assertIn("function openLink(", panel)
        self.assertIn("implicitWidth: button.implicitWidth", panel)
        self.assertIn("implicitHeight: button.implicitHeight", panel)
        self.assertIn("focusTarget: keyCatcher", panel)
        self.assertIn("triggeredOnStart: true", panel)
        self.assertIn("onSelectedIdChanged() { nodePicker.value = root.selectedId }", panel)
        self.assertIn('Button { text: "Dashboard"', panel)

    def test_local_installer_is_repeatable_and_never_leaves_scan_backup(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            bindir, home = root / "bin", root / "home"
            bindir.mkdir()
            log = root / "calls"
            for name in ("omarchy", "omarchy-shell", "sleep"):
                script = bindir / name
                script.write_text("#!/bin/sh\nif [ \"$(basename \"$0\")\" = sleep ]; then exit 0; fi\nif [ \"$1 $2\" = \"plugin list\" ]; then [ \"${CATALOG:-1}\" = 1 ] && echo '[{\"id\":\"projectmushroom.gravedecay\"}]' || echo '[]'; else echo \"$0 $*\" >> \"$CALLS\"; fi\nexit 0\n")
                script.chmod(0o755)
            env = dict(os.environ, PATH=str(bindir) + os.pathsep + os.environ["PATH"],
                       HOME=str(home), CALLS=str(log))
            command = [str(CLIENT / "install-local.sh")]
            self.assertEqual(subprocess.run(command, env=env, capture_output=True).returncode, 0)
            self.assertEqual(subprocess.run(command + ["--nope"], env=env, capture_output=True).returncode, 2)
            self.assertEqual(subprocess.run(command + ["--update"], env=env, capture_output=True).returncode, 0)
            dest = home / ".config" / "omarchy" / "plugins" / "projectmushroom.gravedecay"
            self.assertTrue((dest / "manifest.json").is_file())
            self.assertFalse(list(dest.parent.glob(".projectmushroom.gravedecay.backup.*")))
            (dest / "old-payload").write_text("keep")
            missing = dict(env, CATALOG="0")
            self.assertNotEqual(subprocess.run(command + ["--update"], env=missing, capture_output=True).returncode, 0)
            self.assertEqual((dest / "old-payload").read_text(), "keep")
            self.assertFalse(list(dest.parent.glob(".projectmushroom.gravedecay.*")))
            (dest / "manifest.json").write_text('{"id":"foreign"}')
            self.assertNotEqual(subprocess.run(command + ["--update"], env=env, capture_output=True).returncode, 0)


if __name__ == "__main__":
    unittest.main()
