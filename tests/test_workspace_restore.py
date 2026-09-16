import argparse
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile
import unittest
from importlib.machinery import SourceFileLoader
from unittest.mock import patch

HELPER=Path(__file__).resolve().parents[1]/'bin/grave-workspaces'
module=SourceFileLoader('workspace_restore',str(HELPER)).load_module()

def workspace(slug='alice',identity='100',offset=0):
    return {'id':'ts:'+identity,'login':slug+'@example.test','slug':slug,'role':'developer','enabled':True,
            'projects':[],'ports':{'t3':4810+offset,'term':4910+offset,'dash':5010+offset},'provider':{'llm':False}}

def entry(name,value=b'',kind=tarfile.REGTYPE,link=''):
    info=tarfile.TarInfo(name); info.type=kind; info.linkname=link; info.mode=0o755 if kind==tarfile.DIRTYPE else 0o644
    info.uid=0; info.gid=0; info.size=len(value) if kind==tarfile.REGTYPE else 0
    return info,value

def make_backup(backup,records=None,entries=None,version=2):
    records=records if records is not None else [workspace()]
    entries=entries if entries is not None else [entry('workspaces',kind=tarfile.DIRTYPE),entry('workspaces/alice',kind=tarfile.DIRTYPE),entry('workspaces/alice/dirty',b'dirty work')]
    (backup/'configs').mkdir(parents=True,exist_ok=True)
    with tarfile.open(backup/'configs/grave-platform.tar.gz','w:gz') as archive:
        data=json.dumps({'version':1,'workspaces':records}).encode()
        info,value=entry('config/workspaces.json',data); archive.addfile(info,io.BytesIO(value))
        # Platform scripts and service credentials must never be restored here.
        info,value=entry('scripts/ignored',b'not installed'); archive.addfile(info,io.BytesIO(value))
    with tarfile.open(backup/'configs/workspaces.tar.gz','w:gz') as archive:
        for info,value in entries: archive.addfile(info,io.BytesIO(value))
    manifest={'version':version}
    if version==2:
        manifest['artifacts']={p.relative_to(backup).as_posix():{'size':p.stat().st_size,'sha256':hashlib.sha256(p.read_bytes()).hexdigest()} for p in (backup/'configs').iterdir()}
    (backup/'manifest.json').write_text(json.dumps(manifest))
    return backup

@unittest.skipIf(os.geteuid()==0,'archive extraction deliberately refuses root')
class WorkspaceRestore(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        self.base=Path(temporary.name); self.root=self.base/'appliance'; (self.root/'config').mkdir(parents=True)
        self.backup=make_backup(self.base/'backup')
        self.home=self.root/'workspaces/alice'
        self.env={**os.environ,'GRAVE_ROOT':str(self.root),'GRAVE_WORKSPACE_TEST':'1'}
        self.sentinel=self.base/'outside'; self.sentinel.write_text('never change'); self.sentinel.chmod(0o640)

    def cli(self,*args,ok=True):
        result=subprocess.run([HELPER,*map(str,args)],env=self.env,capture_output=True,text=True,timeout=20)
        self.assertEqual(result.returncode==0,ok,result.stdout+result.stderr)
        return result

    def restore(self,ok=True): return self.cli('restore',self.backup,ok=ok)

    def clear_stage(self):
        shutil.rmtree(self.root/'config/workspace-restores',ignore_errors=True)

    def assert_unpublished(self):
        self.assertFalse(self.home.exists()); self.assertFalse((self.root/'config/workspaces.json').exists())
        self.assertEqual(self.sentinel.read_text(),'never change')
        self.assertEqual(self.sentinel.stat().st_mode&0o777,0o640)
        self.assertEqual(self.sentinel.stat().st_uid,os.geteuid())

    def test_dirty_git_index_untracked_files_and_links_survive_roundtrip(self):
        source=self.base/'source'; repo=source/'workspaces/alice/repos/app'; repo.mkdir(parents=True)
        env={**os.environ,'GIT_AUTHOR_NAME':'Test','GIT_AUTHOR_EMAIL':'test@example.test','GIT_COMMITTER_NAME':'Test','GIT_COMMITTER_EMAIL':'test@example.test','GIT_CONFIG_GLOBAL':'/dev/null','GIT_CONFIG_NOSYSTEM':'1'}
        def git(*args): return subprocess.check_output(['git','-C',str(repo),*args],env=env,stderr=subprocess.DEVNULL)
        git('init'); (repo/'file').write_text('committed'); git('add','.'); git('commit','-m','initial')
        (repo/'file').write_text('staged'); git('add','file'); (repo/'file').write_text('unstaged')
        (repo/'untracked').write_text('keep me'); (repo/'executable').write_text('#!/bin/sh\n'); (repo/'executable').chmod(0o755)
        (repo/'external').symlink_to(self.sentinel)
        os.link(repo/'untracked',repo/'hardlink')
        w=workspace(); w['projects']=[{'name':'app','url':'https://github.com/example/app.git'}]
        make_backup(self.backup,records=[w])
        subprocess.run(['tar','-czf',str(self.backup/'configs/workspaces.tar.gz'),'-C',str(source),'workspaces'],check=True)
        manifest=json.loads((self.backup/'manifest.json').read_text()); path=self.backup/'configs/workspaces.tar.gz'
        manifest['artifacts']['configs/workspaces.tar.gz']={'size':path.stat().st_size,'sha256':hashlib.sha256(path.read_bytes()).hexdigest()}
        (self.backup/'manifest.json').write_text(json.dumps(manifest))
        self.restore(); restored=self.home/'repos/app'
        self.assertEqual((restored/'file').read_text(),'unstaged')
        self.assertEqual(subprocess.check_output(['git','-C',str(restored),'show',':file'],env=env),b'staged')
        self.assertEqual((restored/'untracked').read_text(),'keep me')
        self.assertEqual((restored/'hardlink').read_text(),'keep me')
        self.assertNotEqual((restored/'hardlink').stat().st_ino,(restored/'untracked').stat().st_ino)
        self.assertTrue((restored/'external').is_symlink())
        self.assertEqual((restored/'executable').stat().st_mode&0o777,0o700)
        self.assertEqual((repo/'executable').stat().st_mode&0o777,0o755)
        self.assertEqual(list((self.root/'config/workspace-restores').iterdir()),[])
        self.assertFalse((self.root/'scripts/ignored').exists()); self.cli('doctor')

    def test_rejects_traversal_duplicate_special_files_and_link_descendants(self):
        roots=[entry('workspaces/alice',kind=tarfile.DIRTYPE)]
        attacks=[
            [entry('../outside',b'changed')], [entry('/absolute',b'changed')],
            [entry('workspaces/alice/../../outside',b'changed')],
            [entry('workspaces/bob/secret',b'other workspace')],
            [entry('workspaces/alice/x'),entry('workspaces/alice/x')],
            [entry('workspaces/alice/fifo',kind=tarfile.FIFOTYPE)],
            [entry('workspaces/alice/link',kind=tarfile.SYMTYPE,link=str(self.base)),entry('workspaces/alice/link/outside',b'changed')],
            [entry('workspaces/alice/link',kind=tarfile.LNKTYPE,link='../outside')],
            [entry('workspaces/alice/link',kind=tarfile.LNKTYPE,link='workspaces/bob/file')],
            [entry('workspaces/alice/link',kind=tarfile.LNKTYPE,link='workspaces/alice/link')],
            [entry('workspaces/alice/.claude',kind=tarfile.SYMTYPE,link=str(self.base))],
        ]
        for attack in attacks:
            with self.subTest(name=attack[0][0].name,kind=attack[0][0].type):
                make_backup(self.backup,entries=roots+attack)
                self.restore(ok=False); self.assert_unpublished()
                self.assertIn('restore is incomplete',self.cli('doctor',ok=False).stderr)
                self.clear_stage()

    def test_checksum_corruption_is_detected_again_inside_staging(self):
        path=self.backup/'configs/workspaces.tar.gz'; data=path.read_bytes(); path.write_bytes(data[:-1]+bytes([data[-1]^1]))
        self.restore(ok=False); self.assert_unpublished()

    def test_existing_home_and_symlink_are_not_overwritten(self):
        self.home.mkdir(parents=True); (self.home/'sentinel').write_text('keep')
        self.assertIn('target already exists',self.restore(ok=False).stderr)
        self.assertEqual((self.home/'sentinel').read_text(),'keep')
        shutil.rmtree(self.home); self.home.symlink_to(self.base); self.clear_stage()
        self.assertIn('target already exists',self.restore(ok=False).stderr)
        self.assertEqual(self.sentinel.read_text(),'never change')

    def test_registry_identity_slug_and_port_conflicts_preserve_existing_records(self):
        for w in (workspace('alice','200',1),workspace('other','100',1),workspace('other','200',0)):
            with self.subTest(record=w):
                path=self.root/'config/workspaces.json'; path.write_text(json.dumps({'version':1,'workspaces':[w]})); before=path.read_bytes()
                self.assertIn('duplicate identity, slug, or port',self.restore(ok=False).stderr)
                self.assertEqual(path.read_bytes(),before); self.assertFalse(self.home.exists()); self.clear_stage()

    def test_disjoint_restore_preserves_existing_workspace_and_capability(self):
        self.cli('add','200','bobby@example.test','bobby','--no-llm')
        existing=self.root/'workspaces/bobby/dirty'; existing.write_text('existing dirty work')
        control=self.root/'config/workspace-services/bobby.env'; before=control.read_bytes()
        make_backup(self.backup,records=[workspace(offset=1)])
        self.restore(); self.cli('doctor')
        self.assertEqual(existing.read_text(),'existing dirty work'); self.assertEqual(control.read_bytes(),before)
        self.assertEqual({w['slug'] for w in json.loads((self.root/'config/workspaces.json').read_text())['workspaces']},{'alice','bobby'})

    def test_stale_service_capability_is_not_reused(self):
        services=self.root/'config/workspace-services'; services.mkdir(mode=0o700)
        control=services/'alice.env'; control.write_text('GRAVEDECAY_BACKEND_TOKEN=old'); control.chmod(0o600)
        self.assertIn('service configuration already exists',self.restore(ok=False).stderr)
        self.assertEqual(control.read_text(),'GRAVEDECAY_BACKEND_TOKEN=old'); self.assert_unpublished()

    def test_invalid_registry_and_missing_workspace_are_rejected(self):
        for records,entries in (([],None),([dict(workspace(),provider={})],None),([dict(workspace(),removal={})],None),([workspace()],[entry('workspaces',kind=tarfile.DIRTYPE)])):
            make_backup(self.backup,records=records,entries=entries); self.restore(ok=False); self.assert_unpublished(); self.clear_stage()

    def test_legacy_backup_uses_same_safe_extraction(self):
        make_backup(self.backup,version=1); self.assertIn("legacy backup has no checksums",self.restore().stderr)
        self.assertEqual((self.home/'dirty').read_text(),'dirty work')

    def test_archive_symlink_and_staging_symlink_fail_closed(self):
        archive=self.backup/'configs/workspaces.tar.gz'; archive.unlink(); archive.symlink_to(self.sentinel)
        self.restore(ok=False); self.assert_unpublished(); self.clear_stage()
        (self.root/'config/workspace-restores').symlink_to(self.base)
        self.restore(ok=False); self.assert_unpublished()

    def test_interrupted_publish_keeps_receipt_and_blocks_reapply(self):
        attrs={'ROOT':self.root,'REGISTRY':self.root/'config/workspaces.json','HOME_ROOT':self.root/'workspaces',
               'RESTORE_ROOT':self.root/'config/workspace-restores','MIGRATION_ROOT':self.root/'config/workspace-migrations',
               'SERVICE_ROOT':self.root/'config/workspace-services','PROVIDER_SECRET':self.root/'config/secrets/provider.env'}
        make_backup(self.backup,records=[workspace(),workspace('bobby','200',1)],entries=[entry('workspaces/alice',kind=tarfile.DIRTYPE),entry('workspaces/alice/dirty',b'keep'),entry('workspaces/bobby',kind=tarfile.DIRTYPE)])
        rename=module.rename_exclusive
        def interrupted(source,name,destination,retained):
            if name=='bobby': raise OSError('interrupted')
            rename(source,name,destination,retained)
        with patch.multiple(module,**attrs),patch.dict(os.environ,self.env),patch.object(module,'rename_exclusive',side_effect=interrupted):
            with self.assertRaisesRegex(OSError,'^interrupted$'),contextlib.redirect_stdout(io.StringIO()): module.cmd_restore(argparse.Namespace(backup=self.backup))
        self.assertEqual((self.home/'dirty').read_text(),'keep'); self.assertFalse((self.root/'config/workspaces.json').exists())
        self.assertIn('restore is incomplete',self.cli('doctor',ok=False).stderr)
        self.assertIn('restore is incomplete',self.cli('reapply',ok=False).stderr)
        self.assertIn('restore is incomplete',self.restore(ok=False).stderr)
        stages=list((self.root/'config/workspace-restores').iterdir()); self.assertEqual(len(stages),1)
        self.assertTrue((stages[0]/'content/bobby').is_dir())

if __name__=='__main__': unittest.main()
