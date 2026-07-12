import json, os, subprocess, tempfile, unittest
from pathlib import Path

ROOT=Path(__file__).parents[1]; HELPER=ROOT/"bin/grave-workspaces"
class MultiUserEndToEnd(unittest.TestCase):
    def setUp(self): self.temp=tempfile.TemporaryDirectory(); self.root=Path(self.temp.name)
    def tearDown(self): self.temp.cleanup()
    def cli(self,*args,input=None):
        env={**os.environ,"GRAVE_ROOT":str(self.root),"GRAVE_WORKSPACE_TEST":"1"}
        return subprocess.run([HELPER,*args],env=env,text=True,input=input,capture_output=True,check=True).stdout
    def test_admin_and_two_developers_through_onboarding_and_shared_provider(self):
        self.cli("add","100","owner@example.com","owner","--role","admin")
        self.cli("add","200","alice@example.com","alice")
        self.cli("add","300","bob@example.com","bob")
        remote="https://github.com/example/app.git"
        self.cli("grant","alice","app",remote); self.cli("grant","bob","app",remote)
        self.cli("git-config","alice","--name","Alice","--email","alice@example.com")
        self.cli("git-config","bob","--name","Bob","--email","bob@example.com")
        self.cli("linear-set","alice",input="lin_api_"+"a"*32+"\n")
        self.cli("linear-set","bob",input="lin_api_"+"b"*32+"\n")
        provider="sk-test-"+"p"*32
        self.cli("provider-set",input="OPENAI_API_KEY="+provider+"\n")
        self.cli("provider-policy","revoke","bob")
        status=json.loads(self.cli("status")); by_slug={row["slug"]:row for row in status}
        self.assertTrue(by_slug["owner"]["llm"]); self.assertTrue(by_slug["alice"]["llm"]); self.assertFalse(by_slug["bob"]["llm"])
        self.assertEqual(by_slug["alice"]["projects"],1); self.assertEqual(by_slug["bob"]["projects"],1)
        self.assertNotEqual((self.root/"workspaces/alice/repos/app").resolve(),(self.root/"workspaces/bob/repos/app").resolve())
        self.cli("disable","bob")
        registry=json.loads((self.root/"config/workspaces.json").read_text())
        self.assertFalse(next(w for w in registry["workspaces"] if w["slug"]=="bob")["enabled"])
        audit=(self.root/"logs/audit.jsonl").read_text()
        for event in ("workspace_added","project_granted","integration_login","provider_policy_changed","workspace_disabled"):
            self.assertIn(event,audit)
        self.assertNotIn("lin_api_",audit); self.assertNotIn(provider,audit)
        self.cli("doctor")
    def test_migration_backup_and_recovery_contracts_are_wired(self):
        grave=(ROOT/"bin/grave").read_text(); recovery=(ROOT/"docs/RECOVERY.md").read_text()
        self.assertIn("cmd_multiuser",grave); self.assertIn("--reflink=auto",grave)
        self.assertIn("migration failed; single-user config restored",grave)
        self.assertIn("workspaces.tar.gz",grave); self.assertIn("--include-secrets",grave)
        self.assertIn("grave restore <ts> workspaces",recovery)
        self.assertIn("Secrets are excluded by default",recovery)

if __name__ == "__main__": unittest.main()
