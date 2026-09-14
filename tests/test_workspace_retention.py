import argparse
import errno
from importlib.machinery import SourceFileLoader
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

SCRIPT=Path(__file__).resolve().parents[1]/'bin/grave-workspaces'
ws=SourceFileLoader('workspace_retention',str(SCRIPT)).load_module()

class WorkspaceRetention(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)/'appliance'
        self.env={**os.environ,'GRAVE_ROOT':str(self.root),'GRAVE_WORKSPACE_TEST':'1'}
        self.cli('add','123','alice@example.test','alice','--no-llm')
        self.cli('grant','alice','app','https://example.test/app.git')
        self.home=self.root/'workspaces/alice'
        (self.home/'repos/app/dirty').write_text('keep this')
        self.outside=Path(self.temp.name)/'outside'; self.outside.mkdir(mode=0o750)
        (self.outside/'sentinel').write_text('unchanged')
        self.addCleanup(patch.stopall)
        patch.dict(os.environ,{'GRAVE_WORKSPACE_TEST':'1'}).start()
        for name,value in {'ROOT':self.root,'HOME_ROOT':self.root/'workspaces','REGISTRY':self.root/'config/workspaces.json',
                           'SERVICE_ROOT':self.root/'config/workspace-services'}.items():
            patch.object(ws,name,value).start()

    def cli(self,*args,ok=True):
        result=subprocess.run([SCRIPT,*args],env=self.env,text=True,capture_output=True,timeout=15)
        self.assertEqual(result.returncode==0,ok,result.stderr)
        return result

    def snapshot(self):
        return [(str(p.relative_to(self.outside)),p.lstat().st_mode,p.lstat().st_uid,p.lstat().st_gid,
                 p.read_bytes() if p.is_file() else None) for p in [self.outside,*sorted(self.outside.rglob('*'))]]

    def test_revoke_rejects_symlinked_parents_and_checkout_without_touching_targets(self):
        for relative in ('repos','repos/app','revoked'):
            with self.subTest(relative=relative):
                path=self.home/relative; held=path.with_name(path.name+'.held')
                existed=path.exists()
                if existed: path.rename(held)
                path.symlink_to(self.outside,target_is_directory=True)
                before=self.snapshot()
                self.cli('revoke','alice','app',ok=False)
                self.cli('doctor',ok=False)
                self.assertEqual(self.snapshot(),before)
                self.assertEqual(len(ws.load()['workspaces'][0]['projects']),1)
                path.unlink()
                if existed: held.rename(path)

    def test_revoke_collision_never_replaces_old_archive(self):
        revoked=self.home/'revoked'; revoked.mkdir(mode=0o700)
        retained='app-20260101-000000-'+'a'*16
        old=revoked/retained; old.mkdir(); (old/'old-dirty').write_text('old')
        with patch.object(ws.time,'strftime',return_value='20260101-000000'),patch.object(ws.secrets,'token_hex',return_value='a'*16):
            with self.assertRaises(FileExistsError): ws.WorkspaceTree(self.home).revoke('app')
        self.assertEqual((old/'old-dirty').read_text(),'old')
        self.assertEqual((self.home/'repos/app/dirty').read_text(),'keep this')

    def test_missing_checkout_revoke_is_idempotent(self):
        # Simulate a crash after the move, before the registry grant was removed.
        ws.WorkspaceTree(self.home).revoke('app')
        self.cli('revoke','alice','app'); self.cli('revoke','alice','app')
        retained=list((self.home/'revoked').glob('app-*'))
        self.assertEqual(len(retained),1)
        self.assertEqual((retained[0]/'dirty').read_text(),'keep this')
        self.cli('doctor')

    def test_remove_rejects_home_and_archive_parent_symlinks(self):
        self.cli('disable','alice')
        for path in (self.home,self.root/'backups',self.root/'backups/removed-workspaces'):
            with self.subTest(path=str(path)):
                path.parent.mkdir(parents=True,exist_ok=True)
                held=path.with_name(path.name+'.held'); existed=path.exists()
                if existed: path.rename(held)
                path.symlink_to(self.outside,target_is_directory=True)
                before=self.snapshot()
                self.cli('remove','alice','--confirm','alice',ok=False)
                self.assertEqual(self.snapshot(),before)
                self.assertEqual(len(ws.load()['workspaces']),1)
                path.unlink()
                if existed: held.rename(path)

    def test_remove_retries_after_userdel_failure_and_retains_dirty_data(self):
        self.cli('disable','alice')
        args=argparse.Namespace(workspace='alice',confirm='alice')
        def fail_userdel(*arguments):
            if arguments[0]=='userdel': raise subprocess.CalledProcessError(8,arguments)
        with patch.object(ws,'run',side_effect=fail_userdel):
            with self.assertRaises(subprocess.CalledProcessError): ws.cmd_remove(args)
        record=ws.load()['workspaces'][0]
        archive=self.root/'backups/removed-workspaces'/record['removal']['archive']/'home'
        self.assertFalse(self.home.exists()); self.assertEqual((archive/'repos/app/dirty').read_text(),'keep this')
        self.assertEqual(archive.parent.stat().st_mode&0o777,0o700)
        self.cli('reapply',ok=False); self.cli('doctor',ok=False)
        self.cli('add','123','alice@example.test','alice','--no-llm',ok=False)
        self.assertTrue(json.loads(self.cli('status').stdout)[0]['removal_pending'])
        self.cli('remove','alice','--confirm','alice')
        self.assertEqual(ws.load()['workspaces'],[])
        self.assertEqual((archive/'repos/app/dirty').read_text(),'keep this')
        self.cli('doctor')

    def test_remove_retries_before_rename_and_refuses_conflicting_destinations(self):
        self.cli('disable','alice')
        data=ws.load(); record=data['workspaces'][0]
        with patch.object(ws.os,'rename',side_effect=OSError(errno.EIO,'injected interruption')):
            with self.assertRaises(OSError): ws.archive_workspace(data,record)
        receipt=ws.load()['workspaces'][0]['removal']
        archive=self.root/'backups/removed-workspaces'/receipt['archive']/'home'
        archive.mkdir(mode=0o700); (archive/'old-data').write_text('keep old')
        self.cli('remove','alice','--confirm','alice',ok=False)
        self.assertEqual((archive/'old-data').read_text(),'keep old')
        self.assertEqual((self.home/'repos/app/dirty').read_text(),'keep this')
        (archive/'old-data').unlink(); archive.rmdir()
        self.cli('remove','alice','--confirm','alice')
        self.assertEqual((archive/'repos/app/dirty').read_text(),'keep this')

    def test_cross_filesystem_rename_failure_never_falls_back_to_copy(self):
        self.cli('disable','alice'); data=ws.load(); record=data['workspaces'][0]
        with patch.object(ws.os,'rename',side_effect=OSError(errno.EXDEV,'cross-device')),patch.object(ws.shutil,'move') as move:
            with self.assertRaises(OSError): ws.archive_workspace(data,record)
            move.assert_not_called()
        self.assertEqual((self.home/'repos/app/dirty').read_text(),'keep this')
        self.cli('remove','alice','--confirm','alice')

    def test_retry_after_receipt_save_before_archive_creation(self):
        self.cli('disable','alice'); data=ws.load(); record=data['workspaces'][0]
        save=ws.save
        def interrupt(saved):
            save(saved)
            raise OSError(errno.EIO,'interrupted after durable receipt')
        with patch.object(ws,'save',side_effect=interrupt):
            with self.assertRaises(OSError): ws.archive_workspace(data,record)
        self.assertIn('removal',ws.load()['workspaces'][0])
        self.assertTrue(self.home.exists())
        self.cli('remove','alice','--confirm','alice')
        self.cli('doctor')

    def test_retry_after_account_deletion_before_registry_cleanup(self):
        self.cli('disable','alice'); data=ws.load(); record=data['workspaces'][0]
        archive,_=ws.archive_workspace(data,record)
        with patch.object(ws,'workspace_account',side_effect=KeyError('account already deleted')),patch.object(ws,'run') as run:
            ws.cmd_remove(argparse.Namespace(workspace='alice',confirm='alice'))
            self.assertFalse(any(call.args[0]=='userdel' for call in run.call_args_list))
        self.assertEqual(ws.load()['workspaces'],[])
        self.assertEqual((archive/'repos/app/dirty').read_text(),'keep this')

    def test_doctor_rejects_archive_privacy_drift(self):
        self.cli('disable','alice'); self.cli('remove','alice','--confirm','alice')
        parent=self.root/'backups/removed-workspaces'
        for path in (parent,next(parent.iterdir())):
            path.chmod(0o755)
            self.cli('doctor',ok=False)
            path.chmod(0o700)
        self.cli('doctor')

    def test_slug_reuse_creates_a_separate_archive(self):
        for index in range(2):
            if index: self.cli('add','123','alice@example.test','alice','--no-llm')
            (self.home/'different').write_text(str(index))
            self.cli('disable','alice'); self.cli('remove','alice','--confirm','alice')
        archives=list((self.root/'backups/removed-workspaces').glob('alice-*/home'))
        self.assertEqual(sorted((p/'different').read_text() for p in archives),['0','1'])
        self.cli('doctor')

if __name__=='__main__': unittest.main()
