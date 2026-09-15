#!/usr/bin/env python3
"""Real Unix identities, project revocation, and resumable workspace removal."""
import json
import os
from pathlib import Path
import pwd
import subprocess
import tempfile
import time

ROOT=Path('/srv/dev')
HOME=ROOT/'workspaces/bob'
ACCOUNT=pwd.getpwnam('grave-bob')

def run(*args,check=True,**kwargs):
    return subprocess.run(args,check=check,capture_output=True,text=True,timeout=30,**kwargs)

def bob(*args,**kwargs): return run('runuser','-u','grave-bob','--',*map(str,args),**kwargs)
def cli(*args,**kwargs): return run('grave','__users',*args,**kwargs)
def record(): return next(w for w in json.loads((ROOT/'config/workspaces.json').read_text())['workspaces'] if w['slug']=='bob')

assert os.geteuid()==0, 'run only inside the disposable appliance smoke container'
bob('mkdir',HOME/'repos/retention-test')
bob('python3','-c','import pathlib,sys; pathlib.Path(sys.argv[1]).write_text("dirty-work")',HOME/'repos/retention-test/dirty')
cli('adopt','bob','retention-test','https://example.test/retention.git')
with tempfile.TemporaryDirectory(prefix='grave-retention-',dir='/run') as temporary:
    outside=Path(temporary); outside.chmod(0o750)
    sentinel=outside/'sentinel'; sentinel.write_text('root-owned'); sentinel.chmod(0o600)
    before=(outside.stat(),sentinel.stat(),sentinel.read_bytes())
    bob('ln','-s',outside,HOME/'revoked')
    try:
        assert cli('revoke','bob','retention-test',check=False).returncode!=0
        assert cli('doctor',check=False).returncode!=0
        assert len(record()['projects'])==1 and (HOME/'repos/retention-test/dirty').exists()
        assert sorted(p.name for p in outside.iterdir())==['sentinel']
        after=(outside.stat(),sentinel.stat(),sentinel.read_bytes())
        for left,right in zip(before[:2],after[:2]):
            assert (left.st_mode,left.st_uid,left.st_gid)==(right.st_mode,right.st_uid,right.st_gid)
        assert before[2]==after[2]
    finally: bob('rm',HOME/'revoked')
cli('revoke','bob','retention-test')
retained=list((HOME/'revoked').glob('retention-test-*'))
assert len(retained)==1 and (retained[0]/'dirty').read_text()=='dirty-work'
assert retained[0].stat().st_uid==ACCOUNT.pw_uid
cli('doctor')
cli('disable','bob')
# A process outside the managed units makes userdel fail. The home must already
# be safely archived, the disabled registry entry retained, and retry supported.
sleeper=subprocess.Popen(['runuser','-u','grave-bob','--','sleep','60'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
try:
    for _ in range(50):
        if subprocess.run(['pgrep','-u',str(ACCOUNT.pw_uid),'-x','sleep'],stdout=subprocess.DEVNULL).returncode==0: break
        time.sleep(0.1)
    else: raise AssertionError('removal-interruption fixture did not start')
    result=cli('remove','bob','--confirm','bob',check=False)
    assert result.returncode!=0, 'userdel should refuse the still-running workspace process'
    pending=record()['removal']
    archive=ROOT/'backups/removed-workspaces'/pending['archive']/'home'
    assert not HOME.exists() and (archive/'revoked'/retained[0].name/'dirty').read_text()=='dirty-work'
    assert cli('reapply',check=False).returncode!=0
    assert cli('doctor',check=False).returncode!=0
    assert not HOME.exists(), 'reapply recreated a home during pending removal'
    assert bob('cat',archive/'revoked'/retained[0].name/'dirty',check=False).returncode!=0
finally:
    sleeper.terminate(); sleeper.wait(timeout=10)
cli('remove','bob','--confirm','bob')
assert all(w['slug']!='bob' for w in json.loads((ROOT/'config/workspaces.json').read_text())['workspaces'])
assert (archive/'revoked'/retained[0].name/'dirty').read_text()=='dirty-work'
# Reused numerical credentials must still be blocked by the root-owned parents.
assert run('/usr/bin/cat',str(archive/'revoked'/retained[0].name/'dirty'),check=False,
           user=ACCOUNT.pw_uid,group=ACCOUNT.pw_gid,extra_groups=[]).returncode!=0
for path in (archive.parent,archive.parent.parent):
    assert path.stat().st_uid==0 and path.stat().st_mode&0o777==0o700
cli('doctor')
print('Workspace revoke/removal retention: PASS')
