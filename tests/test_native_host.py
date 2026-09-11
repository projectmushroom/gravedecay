"""Exercise the exact bundled host entry point, including app death and PWA routes."""
import json
import os
import platform
from pathlib import Path
import select
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parents[1]


class NativeHostTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        bundle = self.root / 'bundle'
        bundle.mkdir()
        for name in ('gravedecay.py', 'gravenet.py', 'benchmark.py'):
            shutil.copy(ROOT / 'dashboard' / name, bundle / name)
        shutil.copy(ROOT / 'clients/apple/host/host.py', bundle / 'host.py')
        shutil.copytree(ROOT / 'dashboard/static', bundle / 'static')
        shutil.copytree(ROOT / 'web/net', bundle / 'net')
        self.command = [sys.executable, '-I', '-B', '-u', str(bundle / 'host.py'),
                        '--root', str(self.root / 'data')]
        app = os.environ.get('GRAVEDECAY_TEST_APP')
        if app:
            bundle = Path(app) / 'Contents/Resources/NativeHost'
            for architecture in ('aarch64', 'x86_64'):
                self.assertTrue((bundle / architecture / 'python/bin/python3').is_file())
            architecture = 'aarch64' if platform.machine() == 'arm64' else 'x86_64'
            self.command = [str(bundle / architecture / 'python/bin/python3'), '-I', '-B', '-u',
                            str(bundle / 'host.py'), '--root', str(self.root / 'data')]
        self.environment = dict(os.environ, GRAVEDECAY_ALLOWED_USERS='owner@example.test')

    def start(self, network_port=0):
        error = (self.root / 'errors.log').open('w+')
        self.addCleanup(error.close)
        child = subprocess.Popen(self.command + ['--port', '0', '--network-port', str(network_port)],
                                 env=self.environment, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=error)
        def cleanup():
            if child.poll() is None:
                child.terminate()
                child.wait(timeout=5)
            child.stdin.close()
            child.stdout.close()
        self.addCleanup(cleanup)
        if not select.select([child.stdout], [], [], 40)[0]:
            self.fail('Host did not become ready within 40 seconds')
        line = child.stdout.readline()
        if not line:
            error.seek(0)
            self.fail('Host startup failed: ' + error.read())
        return child, json.loads(line)

    def request(self, port, path, headers=None, data=None):
        req = urllib.request.Request(f'http://127.0.0.1:{port}{path}', headers=headers or {}, data=data)
        return urllib.request.urlopen(req, timeout=15)

    def test_pwa_owner_boundary_fresh_state_and_parent_death(self):
        child, ready = self.start()
        port = ready['port']
        with self.request(port, '/grave/') as response:
            self.assertIn(b'Graveyard', response.read())
        with self.request(port, '/grave/manifest.webmanifest') as response:
            manifest = json.load(response)
            self.assertEqual(urllib.parse.urljoin('/grave/manifest.webmanifest', manifest['start_url']), '/grave/')
            self.assertEqual(manifest['scope'], '/')
        with self.request(port, '/grave/sw.js') as response:
            self.assertEqual(response.headers['Service-Worker-Allowed'], '/')
        with self.request(port, '/grave/api/v1/summary') as response:
            first = json.load(response)
            self.assertEqual(first['links'], {'dashboard': '/grave/', 'network': '/net/'})
        with self.request(port, '/api/state') as response:
            state = json.load(response)
            self.assertTrue(state['macos_native'])
            self.assertFalse(state['macos_agents'])
            self.assertEqual(state['repos'], [])
            self.assertEqual(state['github']['error'], 'restricted')
        for headers in ({}, {'Tailscale-User-Login': 'other@example.test'}):
            with self.assertRaises(urllib.error.HTTPError) as raised:
                self.request(port, '/api/settings', headers, b'{}')
            self.assertEqual(raised.exception.code, 403)
        owner = {'Tailscale-User-Login': 'owner@example.test', 'Content-Type': 'application/json'}
        with self.request(port, '/api/settings', owner, b'{"poll_ms":5000}') as response:
            self.assertEqual(response.status, 200)
        with self.assertRaises(urllib.error.HTTPError) as raised:
            self.request(port, '/api/admin/upgrade', owner, b'{"channel":"release"}')
        self.assertEqual(raised.exception.code, 404)
        self.assertEqual((self.root / 'data/config/secrets/dashboard-local-token').stat().st_mode & 0o777, 0o600)
        # No desktop refresh is involved: HTTP collection must advance on its own.
        time.sleep(5.1)
        with self.request(port, '/api/v1/summary') as response:
            self.assertNotEqual(first['observed_at'], json.load(response)['observed_at'])
        subprocess.run(self.command + ['--port', str(port), '--network-port', str(ready['network_port']), '--check'],
                       env=self.environment, check=True, capture_output=True, timeout=30)
        child.stdin.close()
        child.wait(timeout=5)
        for number in (port, ready['network_port']):
            with socket.socket() as probe:
                self.assertNotEqual(probe.connect_ex(('127.0.0.1', number)), 0)

    def test_network_collision_does_not_leave_dashboard_running(self):
        with socket.socket() as occupied:
            occupied.bind(('127.0.0.1', 0))
            occupied.listen()
            result = subprocess.run(self.command + ['--port', '0', '--network-port', str(occupied.getsockname()[1])],
                                    env=self.environment, input=b'', capture_output=True, timeout=15)
            self.assertNotEqual(result.returncode, 0)
            self.assertNotIn(b'"ready": true', result.stdout)


if __name__ == '__main__':
    unittest.main()
