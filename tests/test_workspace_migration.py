import argparse
import contextlib
import io
import json
import os
from pathlib import Path
import pwd
import subprocess
import tempfile
import unittest
from importlib.machinery import SourceFileLoader
from unittest.mock import patch

HELPER=Path(__file__).resolve().parents[1]/'bin/grave-workspaces'
module=SourceFileLoader('workspace_migration',str(HELPER)).load_module()

@unittest.skipIf(os.geteuid()==0,'owner copy deliberately refuses root')
class OwnerMigration(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        self.root=Path(temporary.name)/'appliance'; self.root.mkdir()
        self.owner=Path(temporary.name)/'owner'; self.owner.mkdir()
        account=pwd.getpwuid(os.geteuid())
        self.account=argparse.Namespace(pw_uid=account.pw_uid,pw_gid=account.pw_gid,pw_dir=str(self.owner))
        self.args=argparse.Namespace(identity='100',login='owner@example.test',slug='owner',owner=account.pw_name,llm=False)
        for name,value in {'ROOT':self.root,'REGISTRY':self.root/'config/workspaces.json',
                           'HOME_ROOT':self.root/'workspaces','SERVICE_ROOT':self.root/'config/workspace-services',
                           'MIGRATION_ROOT':self.root/'config/workspace-migrations',
                           'PROVIDER_SECRET':self.root/'config/secrets/provider.env'}.items():
            replacement=patch.object(module,name,value); replacement.start(); self.addCleanup(replacement.stop)
        (self.root/'config').mkdir()
        env=patch.dict(os.environ,{'GRAVE_ROOT':str(self.root),'GRAVE_WORKSPACE_TEST':'1'})
        env.start(); self.addCleanup(env.stop)
        self.home=self.root/'workspaces/owner'

    def migrate(self):
        with patch.object(module.pwd,'getpwnam',return_value=self.account), contextlib.redirect_stdout(io.StringIO()):
            module.cmd_migrate_owner(self.args)

    def write(self,path,value):
        path.parent.mkdir(parents=True,exist_ok=True); path.write_text(value)

    def test_copies_dirty_repos_and_exact_credential_paths_without_changing_sources(self):
        repo=self.root/'repos/app'; repo.mkdir(parents=True)
        subprocess.run(['git','init',str(repo)],check=True,capture_output=True)
        subprocess.run(['git','-C',str(repo),'remote','add','origin','https://github.com/example/app.git'],check=True)
        self.write(repo/'dirty.txt','uncommitted')
        self.write(repo/'tool','#!/bin/sh\n'); (repo/'tool').chmod(0o755)
        (repo/'link').symlink_to('dirty.txt')
        (repo.parent/'linked-checkout').symlink_to(repo)
        self.write(self.root/'agents/t3code/state.json','session-state')
        self.write(self.owner/'.claude/settings.json','{"model":"keep"}')
        self.write(self.owner/'.codex/auth.json','private-token')
        self.write(self.owner/'.config/gh/hosts.yml','github-token')
        self.write(self.root/'config/secrets/linear.env','LINEAR_API_KEY=lin_api_'+32*'x'+'\n')
        self.migrate()
        self.assertEqual((self.home/'repos/app/dirty.txt').read_text(),'uncommitted')
        self.assertEqual((self.home/'repos/app/tool').stat().st_mode&0o777,0o700)
        self.assertTrue((self.home/'repos/app/link').is_symlink())
        self.assertFalse((self.home/'repos/linked-checkout').exists())
        self.assertEqual((self.home/'state/t3/state.json').read_text(),'session-state')
        self.assertEqual(json.loads((self.home/'.claude/settings.json').read_text())['model'],'keep')
        self.assertEqual((self.home/'.codex/auth.json').read_text(),'private-token')
        self.assertEqual((self.home/'.config/gh/hosts.yml').read_text(),'github-token')
        self.assertFalse((self.home/'.claude/.claude').exists())
        self.assertFalse((self.home/'.codex/.codex').exists())
        self.assertEqual((self.owner/'.claude/settings.json').read_text(),'{"model":"keep"}')
        self.assertEqual((repo/'tool').stat().st_mode&0o777,0o755)
        workspace=module.load()['workspaces'][0]
        self.assertEqual(workspace['role'],'admin'); self.assertEqual(workspace['projects'][0]['name'],'app')
        self.assertEqual(list(module.MIGRATION_ROOT.iterdir()),[])
        with contextlib.redirect_stdout(io.StringIO()): module.cmd_doctor(None)

    def test_existing_directory_or_symlink_is_never_overlaid(self):
        self.home.parent.mkdir(); self.home.mkdir(); self.write(self.home/'sentinel','unchanged')
        with self.assertRaisesRegex(SystemExit,'target already exists'): self.migrate()
        self.assertEqual((self.home/'sentinel').read_text(),'unchanged')
        (self.home/'sentinel').unlink(); self.home.rmdir(); self.home.symlink_to(self.owner)
        with self.assertRaisesRegex(SystemExit,'target already exists'): self.migrate()
        self.assertFalse(module.MIGRATION_ROOT.exists())

    def test_existing_registry_identity_is_rejected(self):
        self.migrate()
        control=(module.SERVICE_ROOT/'owner.env').read_bytes()
        with self.assertRaisesRegex(SystemExit,'fresh identity and slug'): self.migrate()
        self.assertEqual((module.SERVICE_ROOT/'owner.env').read_bytes(),control)

    def test_source_link_failure_retains_private_stage_and_blocks_retry(self):
        (self.owner/'.codex').symlink_to(self.root/'config')
        with self.assertRaisesRegex(SystemExit,'copy failed'): self.migrate()
        self.assertFalse(self.home.exists()); self.assertFalse(module.REGISTRY.exists())
        self.assertEqual(module.MIGRATION_ROOT.stat().st_mode&0o777,0o700)
        with self.assertRaisesRegex(SystemExit,'staging is incomplete'): module.cmd_doctor(None)
        with self.assertRaisesRegex(SystemExit,'staging is incomplete'): self.migrate()

    def test_managed_config_link_rejected_without_touching_target(self):
        sentinel=self.root/'sentinel'; sentinel.write_text('secret')
        (self.owner/'.claude').mkdir(); (self.owner/'.claude/settings.json').symlink_to(sentinel)
        before=sentinel.stat()
        with self.assertRaisesRegex(SystemExit,'copy failed'): self.migrate()
        self.assertEqual(sentinel.read_text(),'secret'); self.assertEqual(sentinel.stat(),before)
        self.assertFalse(self.home.exists())

    def test_publish_race_does_not_replace_target(self):
        rename=module.rename_exclusive
        def race(*args):
            self.home.mkdir(); self.write(self.home/'sentinel','keep')
            rename(*args)
        with patch.object(module,'rename_exclusive',side_effect=race), self.assertRaises(FileExistsError): self.migrate()
        self.assertEqual((self.home/'sentinel').read_text(),'keep')
        self.assertFalse(module.REGISTRY.exists())
        with self.assertRaisesRegex(SystemExit,'staging is incomplete'): module.check_migration_staging()

    def test_provision_failure_keeps_registry_and_receipt(self):
        with patch.object(module,'provision',side_effect=OSError('unavailable')), self.assertRaises(OSError): self.migrate()
        self.assertTrue(self.home.is_dir()); self.assertEqual(module.load()['workspaces'][0]['slug'],'owner')
        with self.assertRaisesRegex(SystemExit,'staging is incomplete'): module.cmd_doctor(None)

    def test_staging_symlink_is_rejected(self):
        module.MIGRATION_ROOT.symlink_to(self.owner)
        with self.assertRaises(OSError): self.migrate()
        self.assertEqual(list(self.owner.iterdir()),[])

if __name__=='__main__': unittest.main()
