import ctypes
import os
import pathlib
import re
import shutil
import subprocess
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
GRAVE = (ROOT / 'bin/grave').read_text()
RAISE = (ROOT / 'raise.sh').read_text()


def function(name):
    return re.search(r'^' + name + r'\(\) \{.*?^\}', GRAVE, re.M | re.S).group(0)


class LeapCompatibilityTests(unittest.TestCase):
    def test_zypper_wins_over_apt_compat_frontend(self):
        script = function('pkg_mgr') + '''
command() { case "$*" in '-v zypper'|'-v apt-get') return 0;; *) return 1;; esac; }
pkg_mgr
'''
        result = subprocess.run(['bash', '-c', script], capture_output=True, text=True)
        self.assertEqual(result.stdout.strip(), 'zypper')
        self.assertLess(RAISE.index('elif command -v zypper'), RAISE.index('elif command -v apt-get'))

    def test_legacy_duration_subset_accepts_examples_and_rejects_options(self):
        for value, valid in [('1h30m', True), ('90m', True), ('0.5h', True),
                             ('--unit=evil', False), ('tomorrow', False), ('1h\n', False), ('', False)]:
            script = 'systemd-analyze() { return 1; }\n' + function('valid_auto_thaw_duration')
            result = subprocess.run(['bash', '-c', script + '\nvalid_auto_thaw_duration "$1"', 'bash', value])
            self.assertEqual(result.returncode == 0, valid, value)

    def test_modern_duration_parser_remains_authoritative(self):
        script = 'systemd-analyze() { [[ "$1" == --help ]] && { echo timespan; return; }; [[ "$2" == accepted ]]; }\n'
        script += function('valid_auto_thaw_duration')
        for value, expected in [('accepted', 0), ('1h', 1)]:
            result = subprocess.run(['bash', '-c', script + '\nvalid_auto_thaw_duration "$1"', 'bash', value])
            self.assertEqual(result.returncode, expected)

    def test_auto_thaw_selects_collect_only_when_supported(self):
        # Execute the real scheduling function with a fake launcher; never
        # create a timer or change this machine's mode in a unit test.
        body = function('cmd_auto_thaw_root').replace('/usr/bin/systemd-run', 'fake_run')
        for supported in (False, True):
            script = '''set -euo pipefail
SUDO_UID=$(id -u); SUDO_GID=$(id -g); AUTO_THAW_UNIT=test
bad() { echo "$*" >&2; }; ok() { :; }
valid_auto_thaw_duration() { return 0; }
fake_run() { printf '%s\\n' "$@"; }
'''
            script += 'systemd-run() { echo "' + ('--collect' if supported else '--quiet') + '"; }\n'
            script += body + '\ncmd_auto_thaw_root schedule 1h30m\n'
            # The function requires root; remove only that guard for CI users.
            script = script.replace('[[ $EUID == 0 ]]', 'true')
            result = subprocess.run(['bash', '-c', script], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual('--collect' in result.stdout.splitlines(), supported)
            self.assertIn('--on-active=1h30m', result.stdout)

    def test_owner_units_use_primary_group(self):
        for path in (ROOT / 'systemd').glob('*.tmpl'):
            self.assertNotIn('Group=@USER@', path.read_text(), path.name)
        self.assertIn('RUN_GROUP=$(id -gn "$RUN_USER")', RAISE)
        self.assertNotIn('$RUN_USER:$RUN_USER', RAISE)

    @unittest.skipUnless(shutil.which('gcc'), 'compiler needed')
    def test_memfd_shim_creates_a_usable_anonymous_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            lib_path = pathlib.Path(tmp) / 'compat.so'
            subprocess.run(['gcc', '-shared', '-fPIC', '-Wall', '-Wextra', '-Werror',
                            '-o', str(lib_path), str(ROOT / 'compat/leap42/memfd.c')], check=True)
            lib = ctypes.CDLL(str(lib_path), use_errno=True)
            lib.memfd_create.argtypes = [ctypes.c_char_p, ctypes.c_uint]
            fd = lib.memfd_create(b'grave-test', 1)
            self.assertGreaterEqual(fd, 0)
            try:
                os.write(fd, b'working')
                os.lseek(fd, 0, os.SEEK_SET)
                self.assertEqual(os.read(fd, 7), b'working')
            finally:
                os.close(fd)

    def test_legacy_seccomp_never_enters_default_compose(self):
        for stack in ('core', 'browsers'):
            self.assertNotIn('seccomp=unconfined', (ROOT / 'docker' / stack / 'compose.yaml').read_text())
        installer = (ROOT / 'compat/leap42/install.sh').read_text()
        self.assertIn('legacy_seccomp=0', installer)
        self.assertIn('if ((legacy_seccomp)); then', installer)
        self.assertIn('"$daemon_version" == 18.09.*', installer)


if __name__ == '__main__':
    unittest.main()
