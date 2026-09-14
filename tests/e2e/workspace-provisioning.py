#!/usr/bin/env python3
"""Real root reapply must not mutate external targets controlled by a workspace."""
import os
from pathlib import Path
import pwd
import subprocess
import tempfile

HOME=Path('/srv/dev/workspaces/bob')
USER=pwd.getpwnam('grave-bob')

def bob(*args):
    subprocess.run(['runuser','-u',USER.pw_name,'--',*map(str,args)],check=True,capture_output=True)

def rejected(*args):
    result=subprocess.run(['grave','__users',*args],text=True,capture_output=True,timeout=30)
    assert result.returncode!=0, f'{args}: accepted unsafe provisioning path'
    assert 'unsafe' in result.stderr, f'{args}: unexpected failure: {result.stderr}'

def snapshot(path):
    result=[]
    for item in [path,*sorted(path.rglob('*'))]:
        info=item.lstat()
        result.append((str(item.relative_to(path)),info.st_mode,info.st_uid,info.st_gid,
                       item.read_bytes() if item.is_file() else None))
    return result

assert os.geteuid()==0, 'run only in the disposable root/systemd appliance'
with tempfile.TemporaryDirectory(prefix='grave-provisioning-',dir='/run') as temporary:
    outside=Path(temporary)
    outside.chmod(0o750)
    sentinel=outside/'settings.json'
    sentinel.write_text('{"root-only":"sentinel"}\n')
    sentinel.chmod(0o600)
    before=snapshot(outside)
    for relative in ('.claude','.codex','config','state/t3','repos','.claude/settings.json','.codex/config.toml'):
        path=HOME/relative
        retained=path.with_name(path.name+'.provisioning-test')
        bob('mv',path,retained)
        try:
            bob('ln','-s',outside if retained.is_dir() else sentinel,path)
            rejected('reapply')
            rejected('doctor')
            assert snapshot(outside)==before, f'{relative}: external target changed'
        finally:
            bob('rm',path)
            bob('mv',retained,path)
    # Wrongly owned directories/files must fail before any automatic repair.
    for relative in ('.claude','.codex/config.toml'):
        path=HOME/relative
        prior=path.stat()
        os.chown(path,0,0)
        try:
            rejected('reapply'); rejected('doctor')
            after=path.stat()
            assert after.st_uid==0 and after.st_gid==0 and after.st_mode==prior.st_mode
        finally:
            os.chown(path,prior.st_uid,prior.st_gid)
    # A new home already reached this point through real useradd/provisioning;
    # repeated provisioning must preserve dirty files and service capability.
    dirty=HOME/'repos/provisioning-dirty.txt'
    bob('touch',dirty)
    control=Path('/srv/dev/config/workspace-services/bob.env')
    def capability():
        return next(line for line in control.read_text().splitlines() if line.startswith('GRAVEDECAY_BACKEND_TOKEN='))
    token_before=capability()
    for _ in range(2):
        subprocess.run(['grave','__users','reapply'],check=True,capture_output=True,timeout=30)
    assert dirty.exists(), 'reapply removed dirty workspace data'
    assert capability()==token_before, 'reapply rotated the backend capability'
    assert control.stat().st_uid==0 and control.stat().st_mode&0o777==0o600
    for relative in ('.claude/settings.json','.codex/config.toml'):
        info=(HOME/relative).stat()
        assert info.st_uid==USER.pw_uid and info.st_mode&0o777==0o600
    bob('rm',dirty)
subprocess.run(['grave','__users','doctor'],check=True)
print('Workspace root provisioning isolation: PASS')
