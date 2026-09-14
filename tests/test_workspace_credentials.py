import importlib.util
from importlib.machinery import SourceFileLoader
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / 'libexec/workspace-env.py'
spec = importlib.util.spec_from_file_location('workspace_env', HELPER)
credentials = importlib.util.module_from_spec(spec)
spec.loader.exec_module(credentials)
workspaces = SourceFileLoader('credential_workspaces', str(ROOT / 'bin/grave-workspaces')).load_module()
KEY = 'lin_api_' + 'a' * 32


class WorkspaceCredentials(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.directory = self.home / 'config/secrets'
        self.directory.mkdir(parents=True, mode=0o700)
        self.secret = self.directory / 'linear.env'

    def write(self, text, name='linear.env'):
        path = self.directory / name
        path.write_text(text)
        path.chmod(0o600)
        return path

    def test_reads_existing_credentials_and_optional_onboarding(self):
        self.assertEqual(credentials.read_values(self.home, 'linear'), {})
        self.write(f'# existing format\nLINEAR_API_KEY="{KEY}"\n')
        self.assertEqual(credentials.read_values(self.home, 'linear'), {'LINEAR_API_KEY': KEY})
        values = {'T3_ACTIVITY_URL': 'http://127.0.0.1:4810', 'T3_ACTIVITY_TOKEN': 'local-token',
                  'T3_ACTIVITY_ENVIRONMENT': 'My workspace', 'T3_ACTIVITY_ENVIRONMENT_ID': 'env-1'}
        self.write('\n'.join(f'{k}="{v}"' for k, v in values.items()), 't3-activity.env')
        self.assertEqual(credentials.read_values(self.home, 'activity'), values)

    def test_rejects_control_assignments_duplicates_and_invalid_values(self):
        for extra in ('PATH=/evil', 'T3_BIN=/evil', 'GRAVEDECAY_BACKEND_TOKEN=evil',
                      'OPENAI_API_KEY=unentitled', f'LINEAR_API_KEY={KEY}'):
            with self.subTest(extra=extra):
                self.write(f'LINEAR_API_KEY={KEY}\n{extra}\n')
                with self.assertRaises(ValueError): credentials.read_values(self.home, 'linear')
        for value in ('lin_api_' + '$' * 32, 'lin_api_' + 'a' * 9000, ''):
            self.write('LINEAR_API_KEY=' + value)
            with self.assertRaises(ValueError): credentials.read_values(self.home, 'linear')

    def test_rejects_symlinks_hardlinks_permissions_and_nonregular_files(self):
        target = self.home / 'outside.env'
        target.write_text(f'LINEAR_API_KEY={KEY}\n')
        target.chmod(0o600)
        self.secret.symlink_to(target)
        with self.assertRaises(OSError): credentials.read_values(self.home, 'linear')
        with self.assertRaises(ValueError): credentials.write_linear(self.home, KEY)
        self.secret.unlink()
        os.link(target, self.secret)
        with self.assertRaises(ValueError): credentials.read_values(self.home, 'linear')
        self.secret.unlink()
        self.write(f'LINEAR_API_KEY={KEY}\n').chmod(0o644)
        with self.assertRaises(ValueError): credentials.read_values(self.home, 'linear')
        self.secret.unlink()
        os.mkfifo(self.secret, 0o600)
        with self.assertRaises(ValueError): credentials.read_values(self.home, 'linear')

    def test_parent_symlink_and_replacement_race_leave_external_target_unchanged(self):
        target = self.home / 'outside'
        target.mkdir(mode=0o700)
        sentinel = target / 'linear.env'
        sentinel.write_text('DO-NOT-CHANGE')
        sentinel.chmod(0o640)
        before = sentinel.stat()
        self.directory.rmdir()
        self.directory.symlink_to(target, target_is_directory=True)
        with self.assertRaises(OSError): credentials.write_linear(self.home, KEY)
        self.directory.unlink()
        self.directory.mkdir(mode=0o700)
        replace = os.replace
        def race(src, dst, **kwargs):
            self.secret.symlink_to(sentinel)
            replace(src, dst, **kwargs)
        with patch.object(credentials.os, 'replace', side_effect=race):
            credentials.write_linear(self.home, KEY)
        self.assertFalse(self.secret.is_symlink())
        self.assertEqual(credentials.read_values(self.home, 'linear')['LINEAR_API_KEY'], KEY)
        self.assertEqual(sentinel.read_text(), 'DO-NOT-CHANGE')
        after = sentinel.stat()
        self.assertEqual((before.st_mode, before.st_uid, before.st_gid),
                         (after.st_mode, after.st_uid, after.st_gid))

    @unittest.skipIf(os.geteuid() == 0, 'loader intentionally refuses root')
    def test_exec_preserves_service_controls_and_does_not_print_secrets_on_error(self):
        code = 'import os,json; print(json.dumps({k:os.environ.get(k) for k in ("LINEAR_API_KEY","T3_BIN")}))'
        command = [sys.executable, HELPER, 'exec', self.home, 'linear', sys.executable, '-c', code]
        env = {**os.environ, 'T3_BIN': '/trusted/t3', 'LINEAR_API_KEY': 'stale'}
        missing = subprocess.run(command, env=env, text=True, capture_output=True, check=True)
        self.assertEqual(json.loads(missing.stdout), {'LINEAR_API_KEY': None, 'T3_BIN': '/trusted/t3'})
        self.write(f'LINEAR_API_KEY={KEY}\n')
        valid = subprocess.run(command, env=env, text=True, capture_output=True, check=True)
        self.assertEqual(json.loads(valid.stdout), {'LINEAR_API_KEY': KEY, 'T3_BIN': '/trusted/t3'})
        self.write(f'LINEAR_API_KEY={KEY}\nT3_BIN=/evil\n')
        invalid = subprocess.run(command, env=env, text=True, capture_output=True)
        self.assertNotEqual(invalid.returncode, 0)
        self.assertEqual(invalid.stdout, '')
        self.assertNotIn(KEY, invalid.stderr)

    def test_doctor_rejects_legacy_or_extra_manager_sources_and_missing_loader(self):
        w = {'slug': 'alice'}
        instance = 'gravedecay-t3@alice.service'
        expected = f'{workspaces.service_env(w)} (ignore_errors=no) {workspaces.provider_link(w)} (ignore_errors=yes)'
        command = f'{workspaces.ROOT}/scripts/workspace-env.py exec {workspaces.HOME_ROOT}/alice linear /usr/bin/t3'
        with patch.object(workspaces.subprocess, 'check_output', side_effect=[expected, command]):
            workspaces.check_unit_credentials(w, instance, 'gravedecay-t3')
        for sources, loader in ((expected + ' /home/alice/evil.env (ignore_errors=yes)', command),
                                ('/home/alice/linear.env (ignore_errors=yes)', command),
                                (expected, '/usr/bin/t3')):
            with self.subTest(sources=sources, loader=loader):
                with patch.object(workspaces.subprocess, 'check_output', side_effect=[sources, loader]):
                    with self.assertRaises(SystemExit):
                        workspaces.check_unit_credentials(w, instance, 'gravedecay-t3')

    def test_manager_inputs_reject_symlink_ancestors_and_files(self):
        with patch.object(workspaces, 'ROOT', self.home), patch.dict(os.environ, GRAVE_WORKSPACE_TEST='1'):
            source = self.write('CAPABILITY=private')
            self.assertTrue(workspaces.check_manager_file(source))
            link = self.home / 'linked'
            link.symlink_to(self.directory, target_is_directory=True)
            with self.assertRaises(SystemExit): workspaces.check_manager_file(link / 'linear.env')
            alias = self.directory / 'alias.env'
            alias.symlink_to(source)
            with self.assertRaises(SystemExit): workspaces.check_manager_file(alias)

    def test_systemd_never_reads_personal_credentials_and_raise_installs_loader(self):
        for kind in ('t3', 'term', 'dashboard'):
            text = (ROOT / f'systemd/gravedecay-{kind}@.service.tmpl').read_text()
            for line in text.splitlines():
                if line.startswith('EnvironmentFile='):
                    self.assertIn('/config/workspace-services/', line)
                    self.assertNotIn('/workspaces/', line)
            self.assertIn('scripts/workspace-env.py exec', text)
        self.assertIn('"$REPO_DIR/libexec/workspace-env.py" "$GRAVE_ROOT/scripts/workspace-env.py"',
                      (ROOT / 'raise.sh').read_text())


if __name__ == '__main__': unittest.main()
