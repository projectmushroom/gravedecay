import json, os, subprocess, tempfile, unittest
from pathlib import Path

SCRIPT=Path(__file__).parents[1]/"bin/grave-workspaces"

class WorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.root=Path(self.temp.name)
    def tearDown(self): self.temp.cleanup()
    def run_cli(self,*args,ok=True):
        env={**os.environ,"GRAVE_ROOT":str(self.root),"GRAVE_WORKSPACE_TEST":"1"}
        p=subprocess.run([SCRIPT,*args],env=env,text=True,capture_output=True)
        self.assertEqual(p.returncode==0,ok,p.stderr)
        return p
    def test_lifecycle_is_idempotent_and_isolated(self):
        self.run_cli("add","123","a@example.com","alice")
        self.run_cli("add","456","b@example.com","bob")
        self.run_cli("add","123","a@example.com","alice")
        data=json.loads((self.root/"config/workspaces.json").read_text())
        self.assertEqual(len(data["workspaces"]),2)
        self.assertNotEqual(data["workspaces"][0]["ports"],data["workspaces"][1]["ports"])
        self.assertTrue((self.root/"workspaces/alice/state/t3").is_dir())
        self.run_cli("disable","alice")
        self.assertFalse(json.loads((self.root/"config/workspaces.json").read_text())["workspaces"][0]["enabled"])
        self.run_cli("remove","alice","--confirm","wrong",ok=False)
        self.run_cli("remove","alice","--confirm","alice")
        self.assertTrue((self.root/"backups/removed-workspaces/alice").is_dir())
    def test_rejects_unsafe_and_duplicate_values(self):
        self.run_cli("add","123","a@example.com","../alice",ok=False)
        self.run_cli("add","123","a@example.com","alice")
        self.run_cli("add","123","a@example.com","bob",ok=False)

if __name__ == "__main__": unittest.main()
