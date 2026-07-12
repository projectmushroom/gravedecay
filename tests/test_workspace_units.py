import unittest
from pathlib import Path

ROOT=Path(__file__).parents[1]
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
        self.assertIn("-i lo",term); self.assertIn("TMUX_SOCKET=grave-%i",term)
        webterm=(ROOT/"bin/webterm").read_text()
        self.assertIn('WORKSPACE_ROLE" == admin',webterm); self.assertIn('tmux -L "$TMUX_SOCKET"',webterm)
        dashboard=(ROOT/"dashboard/gravedecay.py").read_text()
        self.assertIn('os.environ.get("DASH_PORT", "4712")',dashboard)

if __name__ == "__main__": unittest.main()
