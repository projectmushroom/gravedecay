import importlib.util
import io
import json
import os
import pathlib
import pwd
import shutil
import subprocess
import tempfile
import unittest
import urllib.error
from unittest.mock import patch

ROOT = pathlib.Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('connect_diagnose', ROOT / 'libexec/t3-connect-diagnose.py')
diag = importlib.util.module_from_spec(spec)
spec.loader.exec_module(diag)
GRAVE = (ROOT / 'bin/grave').read_text()
FUNCTIONS = GRAVE[GRAVE.index('t3_owner()'):GRAVE.index('t3_connect_json()')]


class ConnectRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = pathlib.Path(self.tmp.name)
        (self.base / 'userdata/secrets').mkdir(parents=True)
        (self.base / 'userdata/logs').mkdir()

    def attempt(self, stamp, outcome):
        return json.dumps({'name': 'environment.cloud.reconcileDesiredLinkWith',
                           'startTimeUnixNano': str(stamp), 'exit': outcome}) + '\n'

    def test_latest_success_supersedes_old_quota_error(self):
        path = self.base / 'userdata/logs/server.trace.ndjson'
        path.write_text(self.attempt(1000000000, {'_tag': 'Failure', 'cause': 'environment_link_limit_exceeded'}) +
                        self.attempt(2000000000, {'_tag': 'Success'}))
        self.assertEqual(diag.latest_link_attempt(self.base)['result'], 'Link reconciliation succeeded')

    def test_generic_403_is_not_reported_as_a_confirmed_quota(self):
        path = self.base / 'userdata/logs/server.trace.ndjson'
        path.write_text(self.attempt(1000000000, {'_tag': 'Failure', 'cause':
            '403 POST https://relay.t3.codes/v1/client/environment-links secret=DO-NOT-PRINT'}))
        with patch('sys.stdout', new_callable=io.StringIO) as out:
            self.assertEqual(diag.main([str(self.base), '--offline']), 0)
        self.assertIn('does not prove a tunnel limit', out.getvalue())
        self.assertNotIn('DO-NOT-PRINT', out.getvalue())
        self.assertNotIn('code', diag.latest_link_attempt(self.base))

    def test_preserved_quota_error_gets_specific_guidance(self):
        (self.base / 'userdata/logs/server.trace.ndjson').write_text(
            self.attempt(1000000000, {'_tag': 'Failure', 'cause': 'environment_link_limit_exceeded'}))
        with patch('sys.stdout', new_callable=io.StringIO) as out:
            diag.main([str(self.base), '--offline'])
        self.assertIn('relay reports a managed-tunnel limit', out.getvalue())

    def test_environment_response_drops_secrets_and_credentials(self):
        (self.base / 'userdata/secrets/cloud-cli-oauth-token.bin').write_text(json.dumps({'accessToken': 'PRIVATE'}))
        class Opener:
            def open(inner, request, timeout):
                self.assertEqual(request.full_url, 'https://relay.t3.codes/v1/environments')
                self.assertEqual(request.get_header('Authorization'), 'Bearer PRIVATE')
                self.assertEqual(timeout, 20)
                self.assertEqual(request.get_header('User-agent'), 'gravedecay-connect-diagnostics/1')
                return io.StringIO(json.dumps({'environments': [{'environmentId': 'id', 'label': 'mac\nname',
                    'endpoint': {'providerKind': 'manual', 'connectorToken': 'PRIVATE'}, 'credential': 'PRIVATE'}]}))
        rows = diag.list_environments(self.base, Opener())
        self.assertEqual(rows, [{'id': 'id', 'label': 'mac\nname', 'provider': 'manual'}])
        self.assertNotIn('PRIVATE', json.dumps(rows))
        self.assertIsNone(diag.NoRedirect().redirect_request(None, None, 302, '', {}, 'https://example.com'))

    def test_api_error_body_is_never_printed(self):
        error = urllib.error.HTTPError('https://relay.t3.codes', 401, 'PRIVATE', {}, None)
        with patch.object(diag, 'list_environments', side_effect=error), \
             patch('sys.stdout', new_callable=io.StringIO), patch('sys.stderr', new_callable=io.StringIO) as err:
            self.assertEqual(diag.main([str(self.base)]), 1)
        self.assertIn('HTTP 401', err.getvalue())
        self.assertNotIn('PRIVATE', err.getvalue())

    def run_functions(self, owner, command):
        env = dict(os.environ, T3_BASE_DIR=str(self.base), TOOL_PATH='')
        script = 'set -euo pipefail\nsystemctl() { printf "User=%s\\n" ' + owner + '; }\n'
        return subprocess.run(['bash', '-c', script + FUNCTIONS + command], env=env, text=True, capture_output=True)

    def test_current_owner_uses_account_home_not_caller_home(self):
        owner = pwd.getpwuid(os.getuid())
        result = self.run_functions(owner.pw_name, 't3_owner_run sh -c \'printf "%s" "$HOME"\'')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, owner.pw_dir)

    @unittest.skipUnless(os.geteuid() == 0 and shutil.which('sudo'), 'root needed for real cross-user regression')
    def test_root_created_private_file_fails_service_probe(self):
        nobody = pwd.getpwnam('nobody')
        for path in (self.base, self.base / 'userdata', self.base / 'userdata/secrets'):
            path.chmod(0o755)
            os.chown(path, nobody.pw_uid, nobody.pw_gid)
        secret = self.base / 'userdata/secrets/cloud-cli-desired-link.bin'
        secret.write_text('managed')
        secret.chmod(0o600)
        failed = self.run_functions('nobody', 't3_connect_files_ok')
        self.assertNotEqual(failed.returncode, 0)
        os.chown(secret, nobody.pw_uid, nobody.pw_gid)
        passed = self.run_functions('nobody', 't3_connect_files_ok')
        self.assertEqual(passed.returncode, 0, passed.stderr)
        secret.chmod(0o644)
        self.assertNotEqual(self.run_functions('nobody', 't3_connect_files_ok').returncode, 0)

    @unittest.skipUnless(os.geteuid() == 0 and shutil.which('sudo'), 'root needed for real cross-user regression')
    def test_root_mode_write_is_owned_by_service_user(self):
        nobody = pwd.getpwnam('nobody')
        self.base.chmod(0o755)
        os.chown(self.base, nobody.pw_uid, nobody.pw_gid)
        mode = self.base / 'config/t3-connect.mode'
        result = self.run_functions('nobody', 'T3_CONNECT_MODE_FILE="$T3_BASE_DIR/config/t3-connect.mode"; t3_connect_set_mode full')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(mode.read_text(), 'full\n')
        self.assertEqual(mode.stat().st_uid, nobody.pw_uid)


if __name__ == '__main__':
    unittest.main()
