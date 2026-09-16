#!/usr/bin/env python3
"""Migration reads as the owner and publishes only a new, isolated workspace."""
import json
import os
from pathlib import Path
import pwd
import shutil
import subprocess
import tempfile

assert os.geteuid()==0, 'run only in the disposable root/systemd appliance'
owner=pwd.getpwnam('mole')
helper='/usr/local/bin/grave-workspaces'
slug='migprobe'
with tempfile.TemporaryDirectory(prefix='grave-migration-',dir='/run') as temporary:
    base=Path(temporary); base.chmod(0o755)
    root=base/'appliance'; root.mkdir(mode=0o755); os.chown(root,owner.pw_uid,owner.pw_gid)
    config=root/'config'; config.mkdir(mode=0o755); os.chown(config,owner.pw_uid,owner.pw_gid)
    state=root/'agents/t3code'; state.mkdir(parents=True)
    secret=state/'root-only'; secret.write_text('never-deliver-root-secret'); secret.chmod(0o600)
    sentinel=base/'sentinel'; sentinel.write_text('unchanged'); sentinel.chmod(0o600)
    before=(sentinel.read_bytes(),sentinel.stat().st_uid,sentinel.stat().st_mode)
    env={**os.environ,'GRAVE_ROOT':str(root),'GRAVE_WORKSPACE_START':'0'}
    env.pop('GRAVE_WORKSPACE_TEST',None)
    def cli(*args,ok=True):
        result=subprocess.run([helper,*args],env=env,capture_output=True,text=True,timeout=60)
        assert (result.returncode==0)==ok, f'{args}: {result.stdout} {result.stderr}'
        assert 'never-deliver-root-secret' not in result.stdout+result.stderr
        return result
    def migrate(ok=True): return cli('migrate-owner','100','mole@example.com',slug,'--owner','mole','--no-llm',ok=ok)
    home=root/'workspaces'/slug
    # A root-only source is unreadable to the copy worker, even though the
    # orchestrator is root. Failure must not create a home, account or registry.
    assert 'copy failed' in migrate(ok=False).stderr
    assert not home.exists() and not (config/'workspaces.json').exists()
    stages=config/'workspace-migrations'
    assert stages.stat().st_uid==0 and stages.stat().st_mode&0o777==0o700
    retained=list(stages.iterdir()); assert len(retained)==1
    assert 'staging is incomplete' in cli('doctor',ok=False).stderr
    assert subprocess.run(['runuser','-u','mole','--','test','-r',str(retained[0]/'record.json')]).returncode!=0
    # Fixture-only cleanup after validating failure; never discard appliance data.
    shutil.rmtree(retained[0]); secret.unlink()
    dirty=state/'dirty'; dirty.write_text('owner-session'); os.chown(dirty,owner.pw_uid,owner.pw_gid)
    (state/'external-link').symlink_to(sentinel)
    home.symlink_to(base)
    assert 'target already exists' in migrate(ok=False).stderr
    home.unlink()
    # An existing Unix account is a conflict even when its home is absent.
    subprocess.run(['useradd','--system','--no-create-home','--home-dir',str(home),'grave-'+slug],check=True)
    try:
        assert 'Unix account already exists' in migrate(ok=False).stderr
    finally: subprocess.run(['userdel','grave-'+slug],check=True)
    try:
        migrate()
        account=pwd.getpwnam('grave-'+slug)
        assert account.pw_uid!=owner.pw_uid and account.pw_uid!=0
        for path in (home,home/'state/t3/dirty',home/'.claude/settings.json',home/'.codex/config.toml'):
            assert path.stat().st_uid==account.pw_uid and not path.stat().st_mode&0o077, str(path)
        assert (home/'state/t3/dirty').read_text()=='owner-session'
        assert (home/'state/t3/external-link').is_symlink()
        assert (sentinel.read_bytes(),sentinel.stat().st_uid,sentinel.stat().st_mode)==before
        assert dirty.stat().st_uid==owner.pw_uid and dirty.read_text()=='owner-session'
        assert list(stages.iterdir())==[]
        assert json.loads((config/'workspaces.json').read_text())['workspaces'][0]['role']=='admin'
        assert 'fresh identity and slug' in migrate(ok=False).stderr
        assert subprocess.run(['runuser','-u','grave-'+slug,'--','cat',str(home/'state/t3/external-link')],capture_output=True).returncode!=0
    finally:
        subprocess.run(['userdel','grave-'+slug],check=False)
print('Workspace owner migration isolation: PASS')
