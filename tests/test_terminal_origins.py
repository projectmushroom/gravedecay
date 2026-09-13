import importlib.util
from pathlib import Path
import re
import shutil
import socket
import subprocess
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('terminal_probe', ROOT / 'tests/e2e/terminal-origins.py')
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)
LAUNCHERS = ['systemd/gravedecay-term.service.tmpl',
             'systemd/gravedecay-term@.service.tmpl', 'docker/portable/start.sh',
             'macos/LaunchAgents/io.gravedecay.term.plist.tmpl']


class TerminalOriginTests(unittest.TestCase):
    def test_every_launcher_requires_origin_validation(self):
        for name in LAUNCHERS:
            with self.subTest(launcher=name):
                self.assertIn('--check-origin', (ROOT / name).read_text())

    @unittest.skipUnless(shutil.which('ttyd'), 'real ttyd is exercised by appliance CI')
    def test_live_handshakes_for_shipped_launchers_and_negative_control(self):
        # Use the actual origin option from each launcher. Both endpoint forms
        # matter: Serve strips /term, while portable nginx preserves it.
        for name in LAUNCHERS + [None]:
            with self.subTest(launcher=name):
                source = (ROOT / name).read_text() if name else ''
                options = ['--check-origin'] if '--check-origin' in source else []
                path = '/term/ws' if '--base-path /term' in source else '/ws'
                if path.startswith('/term/'):
                    options += ['--base-path', '/term']
                with socket.socket() as reservation:
                    for port in range(3900, 3997):
                        try:
                            reservation.bind(('127.0.0.1', port)); break
                        except OSError:
                            continue
                    else:
                        self.fail('no free loopback test port')
                process = subprocess.Popen(['ttyd', '-i', '127.0.0.1', '-p', str(port),
                                            '-W', *options, 'sleep', '30'],
                                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                try:
                    for _ in range(100):
                        self.assertIsNone(process.poll(), 'ttyd exited before readiness')
                        try:
                            with socket.create_connection(('127.0.0.1', port), timeout=.1):
                                break
                        except OSError:
                            time.sleep(.02)
                    if name:
                        probe.verify(port, path)
                    else:
                        self.assertIn(b' 101 ', probe.handshake(port, 'https://hostile.example'))
                        self.assertIn(b' 101 ', probe.handshake(port, None))
                finally:
                    process.terminate(); process.wait(timeout=5)

    def test_doctor_reads_running_process_and_rejects_missing_flag(self):
        grave = (ROOT / 'bin/grave').read_text()
        function = re.search(r'^terminal_origin_ok\(\) \{.*?^\}', grave, re.M | re.S)[0]
        for flag, expected in [('--check-origin', 0), ('--check-origin-fake', 1), ('absent', 1)]:
            process = subprocess.Popen(['python3', '-c', 'import time; time.sleep(10)', flag])
            try:
                setup = f'systemctl() {{ echo {process.pid}; }}\n'
                result = subprocess.run(['bash', '-c', setup + function + '\nterminal_origin_ok'], capture_output=True)
                self.assertEqual(result.returncode, expected)
            finally:
                process.terminate(); process.wait(timeout=5)
        result = subprocess.run(['bash', '-c', 'systemctl() { echo 0; }\n' + function + '\nterminal_origin_ok'])
        self.assertNotEqual(result.returncode, 0)

    def test_macos_status_requires_the_loaded_origin_argument(self):
        source = (ROOT / 'macos/status.sh').read_text()
        block = re.search(r'^if \[ "\$agentsmode" = 1 \]; then\n.*?^fi$', source, re.M | re.S)[0]
        for output, expected in [('\t--check-origin\n', 0), ('\t--check-origin-fake\n', 1), ('', 1)]:
            import shlex
            setup = 'agentsmode=1; uid=100; rc=0\nlaunchctl() { printf %s ' + shlex.quote(output) + '; }\n'
            result = subprocess.run(['sh', '-c', setup + block + '\nexit "$rc"'], capture_output=True)
            self.assertEqual(result.returncode, expected)
