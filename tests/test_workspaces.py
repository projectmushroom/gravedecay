import json, os, subprocess, tempfile, unittest
from pathlib import Path

SCRIPT=Path(__file__).parents[1]/"bin/grave-workspaces"

class WorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.root=Path(self.temp.name)
    def tearDown(self): self.temp.cleanup()
    def run_cli(self,*args,ok=True,input=None):
        env={**os.environ,"GRAVE_ROOT":str(self.root),"GRAVE_WORKSPACE_TEST":"1"}
        p=subprocess.run([SCRIPT,*args],env=env,text=True,capture_output=True,input=input)
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
        env=(self.root/"config/workspace-services/alice.env").read_text()
        self.assertIn('WORKSPACE_HOME="'+str(self.root/"workspaces/alice")+'"',env)
        self.assertIn('T3_PORT="4810"',env)
        self.assertEqual((self.root/"config/workspace-services/alice.env").stat().st_mode & 0o777,0o600)
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
    def test_linear_credentials_are_private_separate_and_revocable(self):
        self.run_cli("add","123","a@example.com","alice"); self.run_cli("add","456","b@example.com","bob")
        self.run_cli("linear-set","alice",input="lin_api_"+"a"*32+"\n")
        self.run_cli("linear-set","bob",input="lin_api_"+"b"*32+"\n")
        alice=self.root/"workspaces/alice/config/secrets/linear.env"; bob=self.root/"workspaces/bob/config/secrets/linear.env"
        self.assertNotEqual(alice.read_text(),bob.read_text()); self.assertEqual(alice.stat().st_mode&0o777,0o600)
        status=self.run_cli("integration-status","alice").stdout
        self.assertIn('"linear": "configured"',status); self.assertNotIn("lin_api_",status)
        self.run_cli("linear-logout","alice"); self.assertFalse(alice.exists()); self.assertTrue(bob.exists())
        self.assertIn('"linear": "onboarding"',self.run_cli("integration-status","alice").stdout)
    def test_invalid_linear_key_is_never_stored(self):
        self.run_cli("add","123","a@example.com","alice")
        p=self.run_cli("linear-set","alice",ok=False,input="not-a-key\n")
        self.assertNotIn("not-a-key",p.stderr)
        self.assertFalse((self.root/"workspaces/alice/config/secrets/linear.env").exists())
    def test_shared_provider_is_single_copy_and_immediately_revocable(self):
        self.run_cli("add","123","a@example.com","alice"); self.run_cli("add","456","b@example.com","bob")
        value="sk-test-"+"z"*32
        out=self.run_cli("provider-set",input="OPENAI_API_KEY="+value+"\n").stdout
        shared=self.root/"config/secrets/provider.env"; a=self.root/"config/workspace-services/alice-provider.env"; b=self.root/"config/workspace-services/bob-provider.env"
        self.assertEqual(shared.stat().st_mode&0o777,0o600); self.assertTrue(a.is_symlink()); self.assertTrue(b.is_symlink())
        self.assertEqual(a.resolve(),shared.resolve()); self.assertEqual(b.resolve(),shared.resolve()); self.assertNotIn(value,out)
        status=self.run_cli("provider-status").stdout; self.assertNotIn(value,status)
        self.run_cli("provider-policy","revoke","bob"); self.assertTrue(a.is_symlink()); self.assertFalse(b.exists())
        data=json.loads((self.root/"config/workspaces.json").read_text()); self.assertFalse(data["workspaces"][1]["provider"]["llm"])
        self.run_cli("provider-policy","grant","bob"); self.assertTrue(b.is_symlink())
    def test_provider_rejects_unknown_environment_names(self):
        self.run_cli("provider-set",ok=False,input="PATH=/evil/credential-value-long\n")
        self.assertFalse((self.root/"config/secrets/provider.env").exists())

if __name__ == "__main__": unittest.main()
