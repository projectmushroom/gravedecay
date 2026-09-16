#!/usr/bin/env python3
"""Real Unix identities: root restore never follows archive links or reuses homes."""
import json
import os
from pathlib import Path
import pwd
import shutil
import subprocess
import sys
import tarfile
import tempfile

sys.path.insert(0,'/repo/tests')
from test_workspace_restore import entry, make_backup, workspace

assert os.geteuid()==0, 'run only in the disposable root/systemd appliance'
owner=pwd.getpwnam('mole'); slug='restprobe'; helper='/usr/local/bin/grave-workspaces'
with tempfile.TemporaryDirectory(prefix='grave-restore-',dir='/run') as temporary:
    base=Path(temporary); base.chmod(0o755)
    root=base/'appliance'; root.mkdir(mode=0o755); os.chown(root,owner.pw_uid,owner.pw_gid)
    config=root/'config'; config.mkdir(mode=0o755); os.chown(config,owner.pw_uid,owner.pw_gid)
    sentinel=base/'root-only'; sentinel.write_text('root-private-sentinel'); sentinel.chmod(0o600)
    before=(sentinel.read_bytes(),sentinel.stat().st_uid,sentinel.stat().st_mode)
    env={**os.environ,'GRAVE_ROOT':str(root),'GRAVE_WORKSPACE_START':'0'}; env.pop('GRAVE_WORKSPACE_TEST',None)
    w=workspace(slug,'100',30)
    backup=base/'backup'
    roots=[entry('workspaces/'+slug,kind=tarfile.DIRTYPE)]
    def cli(*args,ok=True):
        result=subprocess.run([helper,*map(str,args)],env=env,capture_output=True,text=True,timeout=60)
        assert (result.returncode==0)==ok, f'{args}: {result.stdout} {result.stderr}'
        assert 'root-private-sentinel' not in result.stdout+result.stderr
        return result
    def snapshot(): return sentinel.read_bytes(),sentinel.stat().st_uid,sentinel.stat().st_mode
    home=root/'workspaces'/slug
    stages=config/'workspace-restores'
    # Archive link plus a descendant must not redirect a privileged write.
    make_backup(backup,[w],roots+[entry('workspaces/'+slug+'/link',kind=tarfile.SYMTYPE,link=str(base)),entry('workspaces/'+slug+'/link/root-only',b'overwrite')])
    cli('restore',backup,ok=False)
    assert not home.exists() and not (config/'workspaces.json').exists() and snapshot()==before
    assert 'restore is incomplete' in cli('doctor',ok=False).stderr
    assert 'restore is incomplete' in cli('reapply',ok=False).stderr
    retained=list(stages.iterdir()); assert len(retained)==1
    assert stages.stat().st_uid==0 and stages.stat().st_mode&0o777==0o700
    assert subprocess.run(['runuser','-u','mole','--','test','-r',str(retained[0]/'record.json')]).returncode!=0
    shutil.rmtree(stages)  # Fixture-only cleanup, never live appliance data.
    make_backup(backup,[w],roots+[entry('workspaces/'+slug+'/dirty',b'uncommitted'),entry('workspaces/'+slug+'/external',kind=tarfile.SYMTYPE,link=str(sentinel))])
    home.parent.mkdir(); home.symlink_to(base)
    assert 'target already exists' in cli('restore',backup,ok=False).stderr
    assert snapshot()==before
    home.unlink(); shutil.rmtree(stages)
    subprocess.run(['useradd','--system','--no-create-home','--home-dir',str(home),'grave-'+slug],check=True)
    try: assert 'Unix account already exists' in cli('restore',backup,ok=False).stderr
    finally: subprocess.run(['userdel','grave-'+slug],check=True)
    shutil.rmtree(stages)
    # Exercise the public grave restore command using a fixture config, without
    # leaking any GRAVE_ROOT environment override into the helper.
    scripts=root/'scripts'; scripts.mkdir(); shutil.copy('/repo/libexec/backup-integrity.py',scripts/'backup-integrity.py')
    conf=base/'grave.conf'; conf.write_text(f'GRAVE_ROOT={root}\nBACKUP_DIR={base}\n')
    timestamp='20260916-010101'; backup.rename(base/timestamp)
    public_env={**os.environ,'GRAVE_CONF':str(conf)}; public_env.pop('GRAVE_ROOT',None)
    try:
        result=subprocess.run(['grave','restore',timestamp,'workspaces'],env=public_env,capture_output=True,text=True,timeout=60)
        assert result.returncode==0, result.stdout+result.stderr
        account=pwd.getpwnam('grave-'+slug)
        for path in (home,home/'dirty',home/'.claude/settings.json',home/'.codex/config.toml'):
            assert path.stat().st_uid==account.pw_uid and not path.stat().st_mode&0o077, str(path)
        assert (home/'dirty').read_text()=='uncommitted' and snapshot()==before
        assert (home/'external').is_symlink()
        assert subprocess.run(['runuser','-u',account.pw_name,'--','cat',str(home/'external')],capture_output=True).returncode!=0
        assert subprocess.run(['runuser','-u','mole','--','cat',str(home/'dirty')],capture_output=True).returncode!=0
        assert list(stages.iterdir())==[]
        assert json.loads((config/'workspaces.json').read_text())['workspaces'][0]['slug']==slug
        control=config/'workspace-services'/f'{slug}.env'
        assert control.stat().st_uid==0 and control.stat().st_mode&0o777==0o600
        assert not (scripts/'ignored').exists()
        for unit in ('t3','term','dashboard'):
            assert subprocess.run(['systemctl','is-active','--quiet',f'gravedecay-{unit}@{slug}.service']).returncode!=0
    finally: subprocess.run(['userdel','grave-'+slug],check=False)
print('Workspace safe restore isolation: PASS')
