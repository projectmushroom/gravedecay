import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from importlib.machinery import SourceFileLoader
from unittest.mock import patch

HELPER=Path(__file__).resolve().parents[1]/'bin/grave-workspaces'
module=SourceFileLoader('workspace_provisioning',str(HELPER)).load_module()

class WorkspaceProvisioning(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base=Path(self.temp.name)
        self.root=self.base/'appliance'
        self.env={**os.environ,'GRAVE_ROOT':str(self.root),'GRAVE_WORKSPACE_TEST':'1'}
        self.cli('add','123','alice@example.test','alice','--no-llm')
        self.home=self.root/'workspaces/alice'
        self.outside=self.base/'outside'
        self.outside.mkdir(mode=0o750)
        self.sentinel=self.outside/'settings.json'
        self.sentinel.write_text('{"keep":"unchanged"}\n')
        self.sentinel.chmod(0o640)

    def cli(self,*args,ok=True):
        result=subprocess.run([HELPER,*args],env=self.env,text=True,capture_output=True)
        self.assertEqual(result.returncode==0,ok,result.stderr)
        return result

    def snapshot(self):
        return [(str(p.relative_to(self.outside)),p.lstat().st_mode,p.lstat().st_uid,p.lstat().st_gid,
                 p.read_bytes() if p.is_file() else None) for p in [self.outside,*sorted(self.outside.rglob('*'))]]

    def test_reapply_and_doctor_reject_symlinked_managed_directories(self):
        for relative in ('state','state/t3','config','config/secrets','runtime','logs','repos','.claude','.codex'):
            with self.subTest(path=relative):
                path=self.home/relative
                retained=path.with_name(path.name+'.retained')
                path.rename(retained)
                path.symlink_to(self.outside,target_is_directory=True)
                before=self.snapshot()
                self.cli('reapply',ok=False)
                self.cli('doctor',ok=False)
                self.cli('add','123','alice@example.test','alice','--no-llm',ok=False)
                self.assertEqual(self.snapshot(),before)
                path.unlink(); retained.rename(path)

    def test_files_links_and_devices_never_change_external_targets(self):
        for relative in ('.claude/settings.json','.codex/config.toml'):
            with self.subTest(path=relative):
                path=self.home/relative
                original=path.read_bytes()
                before=self.snapshot()
                path.unlink(); path.symlink_to(self.sentinel)
                self.cli('reapply',ok=False); self.cli('doctor',ok=False)
                path.unlink(); os.link(self.sentinel,path)
                self.cli('reapply',ok=False); self.cli('doctor',ok=False)
                path.unlink(); os.mkfifo(path,0o600)
                self.cli('reapply',ok=False); self.cli('doctor',ok=False)
                path.unlink(); path.write_bytes(original); path.chmod(0o600)
                self.assertEqual(self.snapshot(),before)

    def test_home_and_control_file_links_fail_without_adopting_targets(self):
        for path in (self.home,self.root/'config/workspace-services',self.root/'config/workspace-services/alice.env'):
            with self.subTest(path=str(path)):
                retained=path.with_name(path.name+'.retained')
                path.rename(retained)
                path.symlink_to(self.outside if retained.is_dir() else self.sentinel)
                before=self.snapshot()
                self.cli('reapply',ok=False)
                self.cli('doctor',ok=False)
                self.assertEqual(self.snapshot(),before)
                path.unlink(); retained.rename(path)

    def test_directory_mode_drift_is_reported_not_repaired(self):
        path=self.home/'.claude'
        path.chmod(0o777)
        self.cli('reapply',ok=False); self.cli('doctor',ok=False)
        self.assertEqual(path.stat().st_mode&0o777,0o777)

    def test_reapply_preserves_settings_capability_and_dirty_work(self):
        settings=self.home/'.claude/settings.json'
        settings.write_text('{"model":"my-model", "hooks":{"Stop":[{"hooks":[{"command":"my-hook"}]}]}}')
        config=self.home/'.codex/config.toml'
        config.write_text('model = "my-model"\nnotify = ["custom-notifier"]\n')
        dirty=self.home/'repos/dirty.txt'; dirty.write_text('uncommitted')
        capability=(self.root/'config/workspace-services/alice.env').read_text()
        self.cli('reapply'); first=settings.read_text()
        self.cli('reapply'); self.cli('doctor')
        self.assertEqual(settings.read_text(),first)
        self.assertEqual(json.loads(first)['model'],'my-model')
        self.assertIn('my-hook',first)
        self.assertEqual(config.read_text(),'model = "my-model"\nnotify = ["custom-notifier"]\n')
        self.assertEqual(dirty.read_text(),'uncommitted')
        self.assertEqual((self.root/'config/workspace-services/alice.env').read_text(),capability)

    def test_atomic_replacement_race_never_truncates_a_link_target(self):
        path=self.home/'.claude/settings.json'
        before=self.snapshot()
        replace=os.replace
        def race(src,dst,**kwargs):
            path.unlink(); path.symlink_to(self.sentinel)
            replace(src,dst,**kwargs)
        with patch.object(module.os,'replace',side_effect=race):
            module.WorkspaceTree(self.home).write('.claude/settings.json','{"safe":true}\n')
        self.assertFalse(path.is_symlink())
        self.assertEqual(self.snapshot(),before)

    def test_malformed_user_settings_survive_reapply(self):
        settings=self.home/'.claude/settings.json'
        for value in ('{unfinished', '[]', '{"hooks":false}'):
            settings.write_text(value)
            self.cli('reapply')
            self.assertEqual(settings.read_text(),value)

if __name__=='__main__': unittest.main()
