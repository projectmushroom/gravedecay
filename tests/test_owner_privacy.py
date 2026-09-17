import contextlib
import fcntl
import io
import os
from pathlib import Path
import stat
import subprocess
import tempfile
import unittest
from importlib.machinery import SourceFileLoader
from unittest.mock import patch

HELPER=Path(__file__).resolve().parents[1]/'bin/grave-workspaces'
module=SourceFileLoader('owner_privacy',str(HELPER)).load_module()

@unittest.skipIf(os.geteuid()==0,'fixture requires a non-root appliance owner')
class OwnerPrivacy(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.base=Path(temp.name); self.root=self.base/'appliance'; self.root.mkdir()
        self.patch=patch.object(module,'ROOT',self.root); self.patch.start(); self.addCleanup(self.patch.stop)
        env=patch.dict(os.environ,{'GRAVE_BACKUP_DIR':str(self.root/'backups')}); env.start(); self.addCleanup(env.stop)
        for name in (*module.OWNER_TREES,'backups','scripts','web','docs','workspaces'):
            path=self.root/name; path.mkdir(); path.chmod(0o755)
        self.legacy=self.root/'backups/legacy/configs'; self.legacy.mkdir(parents=True)
        self.archive=self.legacy/'old.tar.gz'; self.archive.write_bytes(b'archive unchanged'); self.archive.chmod(0o644)
        self.sentinel=self.base/'sentinel'; self.sentinel.write_text('external unchanged'); self.sentinel.chmod(0o644)
        self.before=self.fingerprint(self.sentinel)

    def fingerprint(self,path):
        info=path.stat(); return path.read_bytes(),info.st_uid,info.st_gid,stat.S_IMODE(info.st_mode)

    def apply(self,check=False,backup_dir=None):
        with contextlib.redirect_stdout(io.StringIO()): module.owner_privacy(check,backup_dir)

    def test_exact_gates_and_idempotent_legacy_repair_preserve_shared_runtime(self):
        owner_data=self.root/'repos/private'; owner_data.write_text('source'); owner_data.chmod(0o644)
        (self.root/'repos/link').symlink_to(self.sentinel)
        self.apply(); self.apply(); self.apply(check=True)
        for name in (*module.OWNER_TREES,'backups'):
            self.assertEqual((self.root/name).stat().st_mode&0o777,0o700)
        for name in ('scripts','web','docs','workspaces'):
            self.assertEqual((self.root/name).stat().st_mode&0o777,0o755)
        self.assertEqual(owner_data.stat().st_mode&0o777,0o644) # no recursive source repair
        self.assertEqual(self.archive.read_bytes(),b'archive unchanged')
        self.assertEqual(self.archive.stat().st_mode&0o777,0o600)
        self.assertEqual(self.legacy.stat().st_mode&0o777,0o700)
        self.assertEqual(self.fingerprint(self.sentinel),self.before)

    def test_read_only_check_detects_mode_and_ownership_drift(self):
        self.apply(); self.archive.chmod(0o644)
        with self.assertRaisesRegex(SystemExit,'owner privacy drift'): self.apply(check=True)
        self.assertEqual(self.archive.stat().st_mode&0o777,0o644)
        self.apply(); (self.root/'agents').chmod(0o755)
        with self.assertRaisesRegex(SystemExit,'owner privacy drift'): self.apply(check=True)
        self.assertEqual((self.root/'agents').stat().st_mode&0o777,0o755)
        self.apply()
        original=module.os.fstat
        def drift(fd):
            info=original(fd)
            if info.st_ino==self.archive.stat().st_ino:
                values=list(info); values[4]=info.st_uid+1000; return os.stat_result(values)
            return info
        with patch.object(module.os,'fstat',side_effect=drift):
            with self.assertRaisesRegex(SystemExit,'unsafe owner privacy'): self.apply()
        self.assertEqual(self.archive.stat().st_uid,os.getuid())

    def test_symbolic_gate_is_refused_without_changing_target(self):
        (self.root/'agents').rmdir(); (self.root/'agents').symlink_to(self.base)
        with self.assertRaises(OSError): self.apply()
        self.assertEqual(self.fingerprint(self.sentinel),self.before)
        self.assertEqual(self.base.stat().st_mode&0o777,0o700)

    def test_linked_backup_files_and_directories_never_change_external_targets(self):
        for kind in ('symlink','hardlink','directory'):
            with self.subTest(kind=kind):
                link=self.legacy/'link'
                if kind=='symlink': link.symlink_to(self.sentinel)
                elif kind=='directory': link.symlink_to(self.base,target_is_directory=True)
                else: os.link(self.sentinel,link)
                with self.assertRaises((OSError,SystemExit)): self.apply()
                self.assertEqual(self.fingerprint(self.sentinel),self.before)
                link.unlink()

    def test_shared_writable_backup_directory_and_fifo_are_rejected(self):
        self.legacy.chmod(0o777)
        with self.assertRaisesRegex(SystemExit,'unsafe owner privacy'): self.apply()
        self.assertEqual(self.legacy.stat().st_mode&0o777,0o777)
        self.legacy.chmod(0o755); fifo=self.legacy/'fifo'; os.mkfifo(fifo)
        with self.assertRaisesRegex(SystemExit,'unsafe legacy backup type'): self.apply()

    def test_retained_workspace_data_is_not_reowned_or_traversed(self):
        retained=self.root/'backups/removed-workspaces'; retained.mkdir(mode=0o700)
        # The real root-only gate is tested with real Unix users in appliance CI.
        (retained/'do-not-walk').symlink_to(self.base)
        self.apply(); self.apply(check=True)
        self.assertTrue((retained/'do-not-walk').is_symlink())
        retained.chmod(0o755)
        with self.assertRaisesRegex(SystemExit,'unsafe workspace directory'): self.apply()
        self.assertEqual(retained.stat().st_mode&0o777,0o755)

    def test_external_backup_directory_is_repaired_but_alias_or_overlap_refused(self):
        external=self.base/'external-backups'; external.mkdir()
        file=external/'archive'; file.write_text('backup'); file.chmod(0o644)
        self.apply(backup_dir=external); self.apply(check=True,backup_dir=external)
        self.assertEqual(file.stat().st_mode&0o777,0o600)
        for path in (self.root,self.base,self.root/'workspaces',self.root/'scripts/backup',self.root/'config',
                     self.root/'backups/removed-workspaces',self.root/'backups/removed-workspaces/alice/home'):
            with self.assertRaises(SystemExit): self.apply(backup_dir=path)
        alias=self.base/'alias'; alias.symlink_to(external)
        with self.assertRaises(OSError): self.apply(backup_dir=alias)

    def test_cli_check_creates_no_locks_and_apply_rejects_linked_registry_lock(self):
        self.apply()
        (self.root/'backups/.backup.lock').unlink()
        env={**os.environ,'GRAVE_ROOT':str(self.root)}
        result=subprocess.run([HELPER,'owner-privacy','--check'],env=env,capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertFalse((self.root/'config/workspaces.lock').exists())
        self.assertFalse((self.root/'backups/.backup.lock').exists())
        (self.root/'config/workspaces.lock').symlink_to(self.sentinel)
        result=subprocess.run([HELPER,'owner-privacy'],env=env,capture_output=True,text=True)
        self.assertNotEqual(result.returncode,0)
        self.assertEqual(self.fingerprint(self.sentinel),self.before)

    def test_busy_backup_does_not_deadlock_or_repair_artifacts(self):
        lock=self.root/'backups/.backup.lock'
        with lock.open('w') as held:
            fcntl.flock(held,fcntl.LOCK_EX)
            with self.assertRaisesRegex(SystemExit,'backup is running'): self.apply()
        self.assertEqual(self.archive.stat().st_mode&0o777,0o644)
        self.apply(); self.apply(check=True)

if __name__=='__main__': unittest.main()
