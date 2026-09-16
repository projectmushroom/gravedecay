import io
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import tarfile
import unittest

import test_backup_integrity as backup_tests
ROOT=backup_tests.ROOT

HELPER=ROOT/'bin/grave-workspaces'

@unittest.skipIf(os.geteuid()==0,'workspace traversal deliberately refuses root')
class WorkspaceBackups(unittest.TestCase):
    setUp_base=backup_tests.BackupRecoveryTests.setUp
    git=backup_tests.BackupRecoveryTests.git
    grave=backup_tests.BackupRecoveryTests.grave
    backup=backup_tests.BackupRecoveryTests.backup
    helper=backup_tests.BackupRecoveryTests.helper

    def setUp(self):
        self.setUp_base()
        self.env.update(GRAVE_ROOT=str(self.root),GRAVE_WORKSPACE_TEST='1',TEST_WORKSPACE_HELPER=str(HELPER))
        (self.root/'tools/sudo').write_text('''#!/bin/sh
if [ "$*" = "-n systemctl --version" ]; then exit 0; fi
if [ "$1" = -n ] && [ "$3" = __users ]; then shift 3; exec "$TEST_WORKSPACE_HELPER" "$@"; fi
exit 98
''')
        self.cli('add','100','alice@example.test','alice','--no-llm')
        self.home=self.root/'workspaces/alice'
        self.credentials={'config/secrets/linear.env':b'LINEAR_API_KEY=private',
                          'config/gh/hosts.yml':b'github-current', '.config/gh/hosts.yml':b'github-legacy',
                          '.codex/auth.json':b'codex-key','.claude/.credentials.json':b'claude-key','.claude.json':b'claude-settings-secret'}
        for name,value in self.credentials.items():
            path=self.home/name; path.parent.mkdir(parents=True,exist_ok=True); path.write_bytes(value); path.chmod(0o600)
        # Regenerated controls are not ordinary platform configuration, even
        # with --include-secrets. Make them unreadable to prove tar skips them.
        (self.root/'config/workspace-services/alice.env').chmod(0)
        self.addCleanup(lambda: (self.root/'config/workspace-services/alice.env').chmod(0o600) if (self.root/'config/workspace-services/alice.env').exists() else None)

    def cli(self,*args,ok=True,env=None):
        result=subprocess.run([HELPER,*map(str,args)],env=env or self.env,capture_output=True,timeout=30)
        self.assertEqual(result.returncode==0,ok,result.stderr.decode())
        return result

    def members(self,path):
        with tarfile.open(path,'r:gz') as archive:
            return {m.name:archive.extractfile(m).read() if m.isreg() else m for m in archive}

    def test_default_excludes_both_github_locations_and_seals_coverage(self):
        (self.home/'dirty').write_text('uncommitted')
        backup=self.backup(); files=self.members(backup/'configs/workspaces.tar.gz')
        self.assertEqual(files['workspaces/alice/dirty'],b'uncommitted')
        for name in self.credentials: self.assertNotIn('workspaces/alice/'+name,files)
        self.assertEqual(json.loads(files['config/workspaces.json'])['workspaces'][0]['slug'],'alice')
        platform=self.members(backup/'configs/grave-platform.tar.gz')
        self.assertNotIn('config/workspaces.json',platform)
        self.assertFalse(any(name.startswith('config/workspace-services') or name=='config/workspaces.lock' for name in platform))
        self.assertEqual(json.loads((backup/'manifest.json').read_text())['workspace_coverage']['workspaces'],[{'id':'ts:100','slug':'alice'}])
        self.cli('backup-check',backup)
        (self.root/'config/workspace-services/alice.env').chmod(0o600)
        self.cli('add','200','bobby@example.test','bobby','--no-llm')
        self.assertIn(b'does not cover',self.cli('backup-check',backup,ok=False).stderr)

    def test_explicit_secret_backup_and_disabled_workspace_are_covered(self):
        self.cli('disable','alice')
        provider=self.root/'config/secrets/provider.env'; provider.parent.mkdir(exist_ok=True)
        provider.write_text('OPENAI_API_KEY=explicit-private-key'); provider.chmod(0o600)
        self.grave('backup','--include-secrets')
        backup=self.backups/self.env['TEST_BACKUP_TS']; files=self.members(backup/'configs/workspaces.tar.gz')
        for name,value in self.credentials.items(): self.assertEqual(files['workspaces/alice/'+name],value)
        self.assertEqual(files['config/secrets/provider.env'],provider.read_bytes())
        self.assertNotIn('config/secrets/provider.env',self.members(backup/'configs/grave-platform.tar.gz'))
        self.cli('backup-check',backup)

    def test_real_backup_restores_staged_and_dirty_git_state_into_fresh_root(self):
        repo=self.home/'repos/app'; shutil.copytree(self.repo,repo)
        self.cli('adopt','alice','app','https://github.com/example/app.git')
        (repo/'example').write_text('staged'); self.git('-C',str(repo),'add','example')
        (repo/'example').write_text('unstaged'); (repo/'untracked').write_text('untracked')
        backup=self.backup()
        target=self.root/'replacement'; (target/'config').mkdir(parents=True)
        self.cli('restore',backup,env={**self.env,'GRAVE_ROOT':str(target)})
        restored=target/'workspaces/alice/repos/app'
        self.assertEqual((restored/'example').read_text(),'unstaged')
        self.assertEqual(self.git('-C',str(restored),'show',':example'),'staged')
        self.assertEqual((restored/'untracked').read_text(),'untracked')
        self.assertFalse((target/'workspaces/alice/config/gh').exists())

    def test_failed_workspace_read_preserves_last_good_backup_and_freshness(self):
        good=self.backup(); before=(self.backups/'.last-verified').read_bytes()
        self.env['TEST_BACKUP_TS']='20260913-020202'
        path=self.home/'unreadable'; path.write_text('private'); path.chmod(0)
        self.addCleanup(path.chmod,0o600)
        self.grave('backup',success=False)
        self.assertTrue(good.exists()); self.assertFalse((self.backups/self.env['TEST_BACKUP_TS']).exists())
        self.assertEqual((self.backups/'.last-verified').read_bytes(),before)
        self.assertEqual((self.backups/'.last-backup').read_text().strip(),good.name)

    def test_links_preserved_without_reading_external_files_and_sockets_skipped(self):
        outside=self.root/'outside'; outside.mkdir(); (outside/'secret').write_text('never-copy')
        (self.home/'external-dir').symlink_to(outside)
        (self.home/'external-file').symlink_to(outside/'secret')
        with socket.socket(socket.AF_UNIX) as sock:
            sock.bind(str(self.home/'runtime/live.sock'))
            result=self.cli('backup-export')
        with tarfile.open(fileobj=io.BytesIO(result.stdout),mode='r:gz') as archive:
            files={m.name:m for m in archive}
        self.assertTrue(files['workspaces/alice/external-dir'].issym())
        self.assertTrue(files['workspaces/alice/external-file'].issym())
        self.assertNotIn('workspaces/alice/external-dir/secret',files)
        self.assertNotIn('workspaces/alice/runtime/live.sock',files)
        self.assertNotIn(b'never-copy',result.stdout)

    def test_missing_or_unregistered_home_fails_without_partial_success(self):
        self.home.rename(self.home.with_name('orphan'))
        self.assertIn(b'differ from registry',self.cli('backup-export',ok=False).stderr)
        self.grave('backup',success=False)
        self.assertFalse((self.backups/'.last-verified').exists())

    def test_coverage_check_rejects_legacy_manifest_without_snapshot(self):
        backup=self.backup(); path=backup/'manifest.json'; manifest=json.loads(path.read_text())
        manifest.pop('workspace_coverage'); path.write_text(json.dumps(manifest))
        self.cli('backup-check',backup,ok=False)

if __name__=='__main__': unittest.main()
