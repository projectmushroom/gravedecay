#!/usr/bin/env python3
"""Nightly owner backup, UID isolation, and recovery into a fresh Unix account."""
import json
import os
from pathlib import Path
import pwd
import shutil
import subprocess
import tarfile
import tempfile
import time

assert os.geteuid()==0, 'run only in the disposable root/systemd appliance'
owner=pwd.getpwnam('mole'); bob=pwd.getpwnam('grave-bob'); root=Path('/srv/dev')
helper='/usr/local/bin/grave-workspaces'

def run(*args,ok=True,env=None,input=None):
    result=subprocess.run(list(map(str,args)),env=env,input=input,capture_output=True,text=True,timeout=90)
    assert (result.returncode==0)==ok, f'{args}: {result.stdout} {result.stderr}'
    return result

def as_user(user,*args,**kwargs): return run('runuser','-u',user,'--',*args,**kwargs)
def read_archive(path):
    with tarfile.open(path,'r:gz') as archive:
        return {m.name:archive.extractfile(m).read() if m.isreg() else m for m in archive}

home=root/'workspaces/bob'
as_user('grave-bob','python3','-c',"""
from pathlib import Path
home=Path('/srv/dev/workspaces/bob')
(home/'repos/backup-dirty').write_text('nightly dirty work')
for relative in ('config/gh/hosts.yml','.config/gh/hosts.yml','.codex/auth.json'):
    path=home/relative; path.parent.mkdir(parents=True,exist_ok=True); path.write_text('explicit-credential-fixture'); path.chmod(0o600)
""")
as_user('mole','cat',home/'repos/backup-dirty',ok=False)
before=(root/'backups/.last-verified').read_bytes()
# This file would be readable if backup traversal accidentally ran as root.
blocked=home/'root-only-backup-test'; blocked.write_text('root-private-backup-sentinel'); blocked.chmod(0o600)
try:
    run('systemctl','start','gravedecay-backup.service',ok=False)
    assert (root/'backups/.last-verified').read_bytes()==before
finally: blocked.unlink()
run('systemctl','reset-failed','gravedecay-backup.service')
run('systemctl','start','gravedecay-backup.service')
assert run('systemctl','show','-p','User','--value','gravedecay-backup.service').stdout.strip()=='mole'
latest=(root/'backups/.last-backup').read_text().strip(); backup=root/'backups'/latest
as_user('mole','grave','backup','verify',latest)
run('grave','__users','backup-check',backup)
manifest=json.loads((backup/'manifest.json').read_text())
assert {w['slug'] for w in manifest['workspace_coverage']['workspaces']}=={'mole','bob'}
files=read_archive(backup/'configs/workspaces.tar.gz')
assert files['workspaces/bob/repos/backup-dirty']==b'nightly dirty work'
for relative in ('config/gh/hosts.yml','.config/gh/hosts.yml','.codex/auth.json'):
    assert 'workspaces/bob/'+relative not in files
as_user('grave-bob','cat',backup/'manifest.json',ok=False)
as_user('grave-mole','cat',backup/'configs/workspaces.tar.gz',ok=False)
# Explicit inclusion works from the owner account too, without relaxing homes.
time.sleep(1.05)
as_user('mole','grave','backup','--include-secrets')
latest=(root/'backups/.last-backup').read_text().strip(); explicit=root/'backups'/latest
files=read_archive(explicit/'configs/workspaces.tar.gz')
assert files['workspaces/bob/config/gh/hosts.yml']==b'explicit-credential-fixture'
assert files['workspaces/bob/.config/gh/hosts.yml']==b'explicit-credential-fixture'
as_user('mole','cat',home/'repos/backup-dirty',ok=False)

# A separate disposable registry allows actual removal/recreation of a Unix
# account while the appliance's two active workspaces stay available.
with tempfile.TemporaryDirectory(prefix='grave-backup-roundtrip-',dir='/run') as temporary:
    base=Path(temporary); base.chmod(0o755)
    appliance=base/'appliance'; appliance.mkdir(); os.chown(appliance,owner.pw_uid,owner.pw_gid)
    for name in ('config','docs','scripts','docker','logs','repos'):
        path=appliance/name; path.mkdir(); os.chown(path,owner.pw_uid,owner.pw_gid)
    shutil.copy('/repo/libexec/backup-integrity.py',appliance/'scripts/backup-integrity.py')
    conf=base/'grave.conf'
    conf.write_text(f'GRAVE_ROOT={appliance}\nBACKUP_DIR={appliance}/backups\nBACKUP_KEEP=2\nAGENT_CONFIG_DIRS=()\nDOCKER_ROOTLESS=1\n')
    env={**os.environ,'GRAVE_ROOT':str(appliance),'GRAVE_CONF':str(conf),'GRAVE_WORKSPACE_START':'0'}
    env.pop('GRAVE_WORKSPACE_TEST',None)
    run(helper,'add','300','bakprobe@example.com','bakprobe','--no-llm',env=env)
    workspace=appliance/'workspaces/bakprobe'; repo=workspace/'repos/probe'
    try:
        as_user('grave-bakprobe','mkdir',repo)
        as_user('grave-bakprobe','git','-C',repo,'init')
        for key,value in (('user.name','Fixture'),('user.email','fixture@example.test')):
            as_user('grave-bakprobe','git','-C',repo,'config',key,value)
        def write(value):
            as_user('grave-bakprobe','python3','-c','from pathlib import Path; import sys; Path(sys.argv[1]).write_text(sys.argv[2])',repo/'file',value)
        write('committed'); as_user('grave-bakprobe','git','-C',repo,'add','file')
        as_user('grave-bakprobe','git','-C',repo,'commit','-m','fixture')
        write('staged'); as_user('grave-bakprobe','git','-C',repo,'add','file'); write('dirty')
        run(helper,'adopt','bakprobe','probe','https://github.com/example/probe.git',env=env)
        outside=base/'root-only'; outside.write_text('outside-secret'); outside.chmod(0o600)
        as_user('grave-bakprobe','ln','-s',outside,repo/'external')
        run('grave','backup',env=env)
        timestamp=(appliance/'backups/.last-backup').read_text().strip()
        saved=appliance/'backups'/timestamp
        run(helper,'backup-check',saved,env=env)
        run(helper,'disable','bakprobe',env=env)
        run(helper,'remove','bakprobe','--confirm','bakprobe',env=env)
        assert not workspace.exists()
        run('grave','restore',timestamp,'workspaces',env=env)
        assert (repo/'file').read_text()=='dirty'
        assert as_user('grave-bakprobe','git','-C',repo,'show',':file').stdout=='staged'
        assert (repo/'external').is_symlink() and outside.read_text()=='outside-secret' and outside.stat().st_uid==0
        as_user('grave-bakprobe','cat',repo/'external',ok=False)
        as_user('mole','cat',repo/'file',ok=False)
        run(helper,'backup-check',saved,env=env)
    finally:
        try: pwd.getpwnam('grave-bakprobe')
        except KeyError: pass
        else: run('userdel','grave-bakprobe')
print('Private workspace nightly backup and restore: PASS')
