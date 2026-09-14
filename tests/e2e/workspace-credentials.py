#!/usr/bin/env python3
"""Adversarial credential restarts on the disposable, raised systemd appliance."""
import os
from pathlib import Path
import pwd
import subprocess
import tempfile
import time

ROOT = Path('/srv/dev')
HOME = ROOT / 'workspaces/bob'
HELPER = ROOT / 'scripts/workspace-env.py'
ACCOUNT = pwd.getpwnam('grave-bob')
KEY = 'lin_api_' + 'test' * 8
UNITS = ['gravedecay-t3@bob.service', 'gravedecay-term@bob.service',
         'gravedecay-dashboard@bob.service']


def run(*args, **kwargs):
    return subprocess.run(args, text=True, capture_output=True, check=True, **kwargs)


def as_bob(*args, **kwargs):
    return run('runuser', '-u', 'grave-bob', '--', *args, **kwargs)


def property_of(unit, name):
    return run('systemctl', 'show', '-p', name, '--value', unit).stdout.strip()


def environment(unit):
    pid = property_of(unit, 'MainPID')
    assert pid.isdecimal() and int(pid) > 0, f'{unit}: no running process'
    return (Path('/proc') / pid / 'environ').read_bytes().split(b'\0')


def private_write(path, text):
    as_bob('python3', '-c',
           'import os,sys; fd=os.open(sys.argv[1],os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o600); '
           'os.write(fd,sys.stdin.read().encode()); os.close(fd)', str(path), input=text)


def failed_restart(unit, marker):
    run('systemctl', 'reset-failed', unit)
    run('systemctl', 'restart', unit)
    for _ in range(50):
        if property_of(unit, 'ExecMainStatus') != '0':
            break
        time.sleep(0.1)
    else:
        raise AssertionError(f'{unit}: unsafe credentials did not fail closed')
    # Check every surviving workspace process, not just a briefly started wrapper.
    for process in Path('/proc').iterdir():
        if not process.name.isdecimal():
            continue
        try:
            if process.stat().st_uid == ACCOUNT.pw_uid:
                assert marker not in (process / 'environ').read_bytes(), 'secret leaked to workspace process'
        except (FileNotFoundError, ProcessLookupError):
            pass
    run('systemctl', 'stop', unit)


assert os.geteuid() == 0, 'this regression runs only in the disposable root/systemd smoke container'
secret = HOME / 'config/secrets/linear.env'
activity = HOME / 'config/secrets/t3-activity.env'
with tempfile.TemporaryDirectory(prefix='grave-credential-test-', dir='/run') as temporary:
    outside = Path(temporary)
    outside.chmod(0o755)
    sentinel = outside / 'secret.env'
    sentinel.write_text('LINEAR_API_KEY=' + KEY + '\nUNENTITLED_SECRET=never-deliver-this-canary\n')
    sentinel.chmod(0o600)
    before = sentinel.stat()
    original = sentinel.read_bytes()
    try:
        # Existing personal credentials keep working after the new templates land.
        as_bob('python3', str(HELPER), 'write-linear', str(HOME), input=KEY)
        private_write(activity, 'T3_ACTIVITY_URL=http://127.0.0.1:4811\nT3_ACTIVITY_TOKEN=activity-test\n'
                      'T3_ACTIVITY_ENVIRONMENT=Bob\nT3_ACTIVITY_ENVIRONMENT_ID=bob\n')
        for unit in UNITS:
            run('systemctl', 'restart', unit)
            for _ in range(50):
                try:
                    values = environment(unit)
                except (AssertionError, FileNotFoundError, ProcessLookupError):
                    time.sleep(0.1)
                    continue
                expected = ('T3_ACTIVITY_TOKEN=activity-test' if 'dashboard' in unit else 'LINEAR_API_KEY=' + KEY).encode()
                if expected in values:
                    break
                time.sleep(0.1)
            else:
                raise AssertionError(f'{unit}: personal credentials not delivered')
            assert not any(v.startswith((b'OPENAI_API_KEY=', b'ANTHROPIC_API_KEY=')) for v in values)
        # Substitute a root-only source as the collaborator, then restart real units.
        for unit in UNITS:
            path = activity if 'dashboard' in unit else secret
            as_bob('rm', '-f', str(path))
            as_bob('ln', '-s', str(sentinel), str(path))
            failed_restart(unit, b'never-deliver-this-canary')
            # The privileged CLI must not write through the same malicious link.
            if path == secret:
                result = subprocess.run(['grave', '__users', 'linear-set', 'bob'], input=KEY,
                                        text=True, capture_output=True)
                assert result.returncode != 0, 'root credential setter accepted a symlink'
                assert KEY not in result.stdout + result.stderr, 'credential printed on error'
            as_bob('rm', str(path))
        # Ordinary files also cannot supply service controls via allowed integrations.
        private_write(secret, 'LINEAR_API_KEY=' + KEY + '\nGRAVEDECAY_BACKEND_TOKEN=forged-control\n')
        failed_restart(UNITS[0], b'GRAVEDECAY_BACKEND_TOKEN=forged-control')
        as_bob('rm', str(secret))
        # Doctor must reject a stale or added PID 1 source, even without a restart.
        dropin = Path('/etc/systemd/system/gravedecay-t3@bob.service.d/credential-regression.conf')
        dropin.parent.mkdir(exist_ok=True)
        dropin.write_text('[Service]\nEnvironmentFile=-' + str(secret) + '\n')
        try:
            run('systemctl', 'daemon-reload')
            result = subprocess.run(['grave', '__users', 'doctor'], text=True, capture_output=True)
            assert result.returncode != 0 and 'unsafe service EnvironmentFiles' in result.stdout + result.stderr
        finally:
            dropin.unlink()
            run('systemctl', 'daemon-reload')
        after = sentinel.stat()
        assert sentinel.read_bytes() == original, 'external sentinel contents changed'
        assert (after.st_mode, after.st_uid, after.st_gid) == (before.st_mode, before.st_uid, before.st_gid), 'external sentinel metadata changed'
    finally:
        for path in (secret, activity):
            as_bob('rm', '-f', str(path))
        for unit in UNITS:
            run('systemctl', 'reset-failed', unit)
            run('systemctl', 'restart', unit)
for port, path in ((4811, '/'), (4911, '/'), (5011, '/healthz')):
    for _ in range(50):
        result = subprocess.run(['curl', '-sf', '--max-time', '1', f'http://127.0.0.1:{port}{path}'],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if result.returncode == 0:
            break
        time.sleep(0.1)
    else:
        raise AssertionError(f'workspace service did not recover on {port}')
run('grave', '__users', 'doctor')
print('Workspace credential restart isolation: PASS')
