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
        env=(self.root/"workspaces/alice/config/service.env").read_text()
        self.assertIn('WORKSPACE_HOME="'+str(self.root/"workspaces/alice")+'"',env)
        self.assertIn('T3_PORT="4810"',env)
        self.assertEqual((self.root/"workspaces/alice/config/service.env").stat().st_mode & 0o777,0o600)
        self.run_cli("disable","alice")
        self.assertFalse(json.loads((self.root/"config/workspaces.json").read_text())["workspaces"][0]["enabled"])
        self.run_cli("remove","alice","--confirm","wrong",ok=False)
        self.run_cli("remove","alice","--confirm","alice")
        self.assertTrue((self.root/"backups/removed-workspaces/alice").is_dir())
    def test_rejects_unsafe_and_duplicate_values(self):
        self.run_cli("add","123","a@example.com","../alice",ok=False)
        self.run_cli("add","123","a@example.com","alice")
        self.run_cli("add","123","a@example.com","bob",ok=False)
    def test_project_grants_use_independent_checkouts_and_preserve_revoked_work(self):
        self.run_cli("add","123","a@example.com","alice")
        self.run_cli("add","456","b@example.com","bob")
        url="https://github.com/example/project.git"
        self.run_cli("grant","alice","project",url); self.run_cli("grant","bob","project",url)
        alice=self.root/"workspaces/alice/repos/project"; bob=self.root/"workspaces/bob/repos/project"
        self.assertTrue(alice.is_dir()); self.assertTrue(bob.is_dir()); self.assertNotEqual(alice.resolve(),bob.resolve())
        (alice/"dirty.txt").write_text("keep me")
        self.run_cli("grant","alice","project",url)
        self.run_cli("revoke","alice","project")
        retained=list((self.root/"workspaces/alice/revoked").glob("project-*"))
        self.assertEqual(len(retained),1); self.assertEqual((retained[0]/"dirty.txt").read_text(),"keep me")
        data=json.loads((self.root/"config/workspaces.json").read_text())
        self.assertEqual(data["workspaces"][0]["projects"],[])
        self.assertEqual(data["workspaces"][1]["projects"],[{"name":"project","url":url}])
    def test_project_validation_rejects_paths_and_unsafe_remotes(self):
        self.run_cli("add","123","a@example.com","alice")
        self.run_cli("grant","alice","../project","https://github.com/x/y.git",ok=False)
        self.run_cli("grant","alice","project","file:///etc",ok=False)
    def test_git_identity_is_private_to_each_workspace_home(self):
        self.run_cli("add","123","a@example.com","alice"); self.run_cli("add","456","b@example.com","bob")
        self.run_cli("git-config","alice","--name","Alice Dev","--email","alice@example.com")
        self.run_cli("git-config","bob","--name","Bob Dev","--email","bob@example.com","--signing-key","ABC123")
        alice=(self.root/"workspaces/alice/.gitconfig").read_text(); bob=(self.root/"workspaces/bob/.gitconfig").read_text()
        self.assertIn("Alice Dev",alice); self.assertNotIn("Bob Dev",alice)
        self.assertIn("Bob Dev",bob); self.assertIn("ABC123",bob); self.assertIn("gpgsign = true",bob)

if __name__ == "__main__": unittest.main()
