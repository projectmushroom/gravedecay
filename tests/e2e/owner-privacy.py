#!/usr/bin/env python3
"""Real Unix-user checks, run only inside the disposable appliance."""
import os
from pathlib import Path
import pwd
import stat
import subprocess
import sys

assert os.geteuid()==0, 'run only in the disposable appliance'
root=Path('/srv/dev'); owner=pwd.getpwnam('mole')
trees=('repos','agents','worktrees','logs','config','docker')
legacy=root/'backups/legacy-privacy-fixture'

def run(*args,ok=True):
    result=subprocess.run(list(map(str,args)),capture_output=True,text=True,timeout=90)
    assert (result.returncode==0)==ok, f'{args}: {result.stdout} {result.stderr}'
    return result

def as_user(user,*args,**kwargs): return run('runuser','-u',user,'--',*args,**kwargs)
def fingerprint(path):
    info=path.stat(); return path.read_bytes(),info.st_uid,info.st_gid,stat.S_IMODE(info.st_mode)

if sys.argv[1]=='prepare':
    # Reproduce an old installation's readable data and root-created backup.
    for name in trees:
        directory=root/name; directory.mkdir(exist_ok=True); directory.chmod(0o755)
        os.chown(directory,owner.pw_uid,owner.pw_gid)
        file=directory/'owner-privacy-fixture'; file.write_text('owner-private-'+name); file.chmod(0o644)
        os.chown(file,owner.pw_uid,owner.pw_gid)
    (legacy/'configs').mkdir(parents=True)
    for directory in (root/'backups',legacy,legacy/'configs'): directory.chmod(0o755)
    file=legacy/'configs/old.tar.gz'; file.write_bytes(b'legacy backup unchanged'); file.chmod(0o644)
    print('Prepared legacy owner privacy fixtures')
else:
    files=[root/name/'owner-privacy-fixture' for name in trees]+[legacy/'configs/old.tar.gz']
    for file in files:
        as_user('mole','cat',file)
        for collaborator in ('grave-bob','grave-mole'):
            as_user(collaborator,'cat',file,ok=False)
    for path in (legacy,legacy/'configs',legacy/'configs/old.tar.gz'):
        assert path.stat().st_uid==owner.pw_uid
        assert path.stat().st_mode&0o777==(0o700 if path.is_dir() else 0o600)
    assert (legacy/'configs/old.tar.gz').read_bytes()==b'legacy backup unchanged'
    for relative in ('scripts/webterm','scripts/workspace-env.py','web/term/index.html','docs/MULTIUSER.md'):
        as_user('grave-bob','test','-r',root/relative)
    as_user('mole','grave','users','owner-privacy','--check')

    # Doctor must identify privacy drift without silently fixing it.
    (root/'agents').chmod(0o755)
    assert 'owner privacy drift: agents' in as_user('mole','grave','users','doctor',ok=False).stderr
    assert (root/'agents').stat().st_mode&0o777==0o755
    as_user('mole','grave','users','reapply')
    as_user('mole','grave','users','owner-privacy','--check')
    as_user('mole','grave','users','doctor')

    # Root repair must not touch an outside inode via a legacy artifact link.
    sentinel=Path('/run/owner-privacy-sentinel'); sentinel.write_bytes(b'external root sentinel'); sentinel.chmod(0o644)
    before=fingerprint(sentinel)
    alias=legacy/'symlink'
    try:
        alias.symlink_to(sentinel)
        as_user('mole','grave','users','owner-privacy',ok=False)
        assert fingerprint(sentinel)==before
        alias.unlink()
        # /run may be a different filesystem, so use an independent inode in
        # the same /srv tree for the hardlink regression.
        outside=root/'privacy-sentinel'; outside.write_bytes(b'outside backup tree'); outside.chmod(0o644)
        outside_before=fingerprint(outside)
        try:
            os.link(outside,alias)
            as_user('mole','grave','users','owner-privacy',ok=False)
            assert fingerprint(outside)==outside_before
            alias.unlink()
        finally: outside.unlink()
        alias.write_bytes(b'foreign owner'); alias.chmod(0o644)
        bob=pwd.getpwnam('grave-bob'); os.chown(alias,bob.pw_uid,bob.pw_gid)
        foreign_before=fingerprint(alias)
        as_user('mole','grave','users','owner-privacy',ok=False)
        assert fingerprint(alias)==foreign_before
    finally:
        alias.unlink(missing_ok=True); sentinel.unlink()
    as_user('mole','grave','users','owner-privacy')
    # Reapply keeps real services usable, including shared scripts/web assets.
    for port in (4810,4910,5010):
        run('curl','--retry','10','--retry-connrefused','--retry-delay','1','-sf',f'http://127.0.0.1:{port}/'+('healthz' if port==5010 else ''))
    print('Owner state and legacy backup Unix-user isolation: PASS')
