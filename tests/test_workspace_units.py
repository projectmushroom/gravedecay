import unittest
from importlib.machinery import SourceFileLoader
from pathlib import Path

ROOT=Path(__file__).parents[1]
WORKSPACES=SourceFileLoader("grave_workspaces", str(ROOT/"bin/grave-workspaces")).load_module()
class WorkspaceUnitContracts(unittest.TestCase):
    def test_units_run_as_workspace_unix_identity(self):
        for name in ("gravedecay-t3@.service.tmpl","gravedecay-term@.service.tmpl","gravedecay-dashboard@.service.tmpl"):
            text=(ROOT/"systemd"/name).read_text()
            self.assertIn("User=grave-%i",text); self.assertIn("Group=grave-%i",text)
            self.assertIn("NoNewPrivileges=yes",text); self.assertIn("UMask=0077",text)
            self.assertIn("TasksMax=",text); self.assertIn("MemoryMax=",text); self.assertIn("CPUQuota=",text)
    def test_t3_and_terminal_state_is_private_and_scoped(self):
        t3=(ROOT/"systemd/gravedecay-t3@.service.tmpl").read_text()
        self.assertIn("workspaces/%i/state/t3",t3); self.assertIn("--host 127.0.0.1",t3)
        term=(ROOT/"systemd/gravedecay-term@.service.tmpl").read_text()
        self.assertIn("-i 127.0.0.1",term); self.assertIn("TMUX_SOCKET=grave-%i",term)
        webterm=(ROOT/"bin/webterm").read_text()
        self.assertIn('WORKSPACE_ROLE" == admin',webterm); self.assertIn('tmux -L "$TMUX_SOCKET"',webterm)
        dashboard=(ROOT/"dashboard/gravedecay.py").read_text()
        self.assertIn('os.environ.get("DASH_PORT", "4712")',dashboard)
        helper=(ROOT/"bin/grave-workspaces").read_text()
        self.assertIn('"GRAVEDECAY_BACKEND_TOKEN":token',helper)
        self.assertIn("missing backend capability",helper)

    def test_later_workspace_lifecycle_uses_the_resolved_appliance_tools(self):
        grave=(ROOT/"bin/grave").read_text()
        self.assertIn('export GRAVE_T3_BIN="$(command -v t3 2>/dev/null || true)"',grave)
        self.assertIn('export GRAVE_TTYD_BIN="$(command -v ttyd 2>/dev/null || true)"',grave)
        self.assertIn('export GRAVE_BIN="$0"',grave)

    def test_workspace_listener_parser_accepts_ipv4_and_ipv6_loopback_only(self):
        self.assertTrue(WORKSPACES.loopback_listeners("LISTEN 0 10 127.0.0.1:4910 0.0.0.0:*\nLISTEN 0 10 [::1]:4910 [::]:*"))
        self.assertFalse(WORKSPACES.loopback_listeners("LISTEN 0 10 *:4910 *:*"))

if __name__ == "__main__": unittest.main()
