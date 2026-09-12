"""Exercise CLI replacement without requiring root on the test runner.

The filesystem tests relocate the helper's two fixed destinations and bypass
only its root guard. The real CLI guard is tested separately; the appliance
smoke test exercises real sudo, root ownership, and changed CLI contents.
"""
import os
from pathlib import Path
import shlex
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
GRAVE = (ROOT / "bin/grave").read_text()
RAISE = (ROOT / "raise.sh").read_text()
GUARD = '[[ $EUID == 0 ]] || { bad "internal CLI installer requires root"; exit 1; }'


class CliInstallTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.grave = self.bin / "grave"
        self.conf = self.root / "grave.conf"
        self.conf.write_text(f'GRAVE_ROOT={shlex.quote(str(self.root))}\n')
        self.env = dict(os.environ, GRAVE_CONF=str(self.conf))
        # Changes apply to this test copy only, never the installed appliance.
        self.grave.write_text(GRAVE.replace(GUARD, 'true').replace('/usr/local/bin/', str(self.bin) + '/'))
        self.grave.chmod(0o755)

    def tearDown(self):
        self.tmp.cleanup()

    def install(self, name, data):
        return subprocess.run([str(self.grave), '__install-cli', name], input=data,
                              env=self.env, capture_output=True)

    def test_real_helper_denies_unprivileged_callers(self):
        if os.geteuid() == 0:
            self.skipTest('requires an unprivileged runner')
        result = subprocess.run([str(ROOT / 'bin/grave'), '__install-cli', '--check'],
                                env=self.env, capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('requires root', result.stdout)

    def test_replacement_is_atomic_and_does_not_execute_the_payload(self):
        dest = self.bin / 'grave-workspaces'
        dest.write_bytes(b'old executable\n')
        marker = self.root / 'must-not-run'
        payload = f'#!/usr/bin/env python3\nopen({str(marker)!r}, "w").write("executed")\n'.encode()
        with dest.open('rb') as old:
            result = self.install('grave-workspaces', payload)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(old.read(), b'old executable\n')
        self.assertEqual(dest.read_bytes(), payload)
        self.assertEqual(dest.stat().st_mode & 0o777, 0o755)
        self.assertFalse(marker.exists())
        self.assertEqual(list(self.bin.glob('.gravedecay-install.*')), [])

    def test_invalid_inputs_leave_the_installed_cli_intact(self):
        dest = self.bin / 'grave-workspaces'
        dest.write_bytes(b'old executable\n')
        for payload in (b'', b'#!/usr/bin/env bash\ntrue\n',
                        b'#!/usr/bin/env python3\nif broken syntax\n', b'x' * 4194305):
            with self.subTest(payload_size=len(payload)):
                self.assertNotEqual(self.install('grave-workspaces', payload).returncode, 0)
                self.assertEqual(dest.read_bytes(), b'old executable\n')
                self.assertEqual(list(self.bin.glob('.gravedecay-install.*')), [])
        original = self.grave.read_bytes()
        self.assertNotEqual(self.install('grave', b'#!/usr/bin/env bash\nif\n').returncode, 0)
        self.assertEqual(self.grave.read_bytes(), original)

    def test_only_named_destinations_are_allowed_and_probe_does_not_write(self):
        before = {p.name: p.read_bytes() for p in self.bin.iterdir()}
        for name in ('../escape', '/etc/sudoers', 'other-cli'):
            self.assertNotEqual(self.install(name, b'new content').returncode, 0)
        self.assertEqual(self.install('--check', b'').returncode, 0)
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.bin.iterdir()})

    def test_changed_cli_installs_with_only_the_grave_sudo_grant(self):
        tools = self.root / 'tools'
        tools.mkdir()
        sudo = tools / 'sudo'
        sudo.write_text('''#!/usr/bin/env bash
set -eu
[[ "$1" == -n ]] && shift
[[ "$1" == "$GRAVE_BIN" ]] || { echo "out-of-scope sudo" >&2; exit 1; }
exec "$@"
''')
        sudo.chmod(0o755)
        # Same helper ABI, changed CLI contents; then invoke the newly replaced
        # executable to update the workspace CLI in the same re-raise.
        next_grave = self.root / 'next-grave'
        next_grave.write_text(self.grave.read_text() + '\n# next release\n')
        next_workspace = self.root / 'next-workspaces'
        next_workspace.write_text('#!/usr/bin/env python3\n# next release\n')
        funcs = RAISE[RAISE.index('same_file() {'):RAISE.index('provision_agent_hooks() {')]
        funcs = funcs.replace('/usr/local/bin/', str(self.bin) + '/')
        script = 'set -euo pipefail\n' + funcs + '\ninstall_cli "$1" "$GRAVE_BIN"\ninstall_cli "$2" "$3"\n'
        env = dict(self.env, PATH=str(tools) + ':' + os.environ['PATH'],
                   IMMUTABLE='0', GRAVE_BIN=str(self.grave), RUN_USER='test', REPO_DIR=str(self.root))
        result = subprocess.run(['bash', '-c', script, 'test', str(next_grave), str(next_workspace),
                                 str(self.bin / 'grave-workspaces')], env=env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.grave.read_bytes(), next_grave.read_bytes())
        self.assertEqual((self.bin / 'grave-workspaces').read_bytes(), next_workspace.read_bytes())

    def test_old_cli_reports_interactive_bootstrap_before_attempting_install(self):
        # No helper and no blanket passwordless sudo: fail clearly, without
        # trying an interactive password prompt in a service.
        tools = self.root / 'tools'
        tools.mkdir()
        sudo = tools / 'sudo'
        log = self.root / 'sudo.log'
        sudo.write_text('#!/bin/sh\nprintf "%s\\n" "$*" >> "$SUDO_TEST_LOG"\nexit 1\n')
        sudo.chmod(0o755)
        funcs = RAISE[RAISE.index('same_file() {'):RAISE.index('provision_agent_hooks() {')]
        env = dict(self.env, PATH=str(tools) + ':' + os.environ['PATH'], IMMUTABLE='0',
                   GRAVE_BIN=str(self.grave), RUN_USER='test', REPO_DIR=str(self.root), SUDO_TEST_LOG=str(log))
        result = subprocess.run(['bash', '-c', 'set -euo pipefail\n' + funcs + '\ninstall_cli "$1" /usr/local/bin/grave',
                                 'test', str(ROOT / 'bin/grave')], stdin=subprocess.DEVNULL,
                                env=env, capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('one-time interactive repair', result.stdout)
        self.assertNotIn('install -m', log.read_text())


if __name__ == '__main__':
    unittest.main()
