"""Denied-viewer contract across every platform, mode and HTTP entry point."""
from contextlib import ExitStack
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest import mock
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
PRIVATE_COLLECTORS = ('load_settings', 'collect_services', 'collect_docker', 'collect_tmux',
    'collect_github', 'collect_ci', 'collect_linear', 'collect_agent_usage', 'collect_repos',
    'collect_macos_repo_inventory', 'collect_macos_work', 'collect_journal', 'collect_backups',
    'collect_inbox', 'collect_agent_history', 'collect_scheduled_runs', 'dispatch_capabilities',
    'agent_worktree_metadata', 'notify_state', 'boot_mode', 'gamewatch_state', 'keepalive_state')


class PublicStateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        with mock.patch.dict(os.environ, {'GRAVE_ROOT': self.temp.name,
                'GRAVEDECAY_ALLOWED_USERS': 'owner@example.test', 'GRAVEDECAY_BASE': '',
                'GRAVEDECAY_PLATFORM': 'linux', 'GRAVEDECAY_BACKEND_TOKEN': '',
                'GRAVEDECAY_REQUIRE_BACKEND_TOKEN': '0'}):
            spec = importlib.util.spec_from_file_location('public_probe', ROOT / 'dashboard/gravedecay.py')
            self.dash = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(self.dash)

    def test_platform_matrix_never_collects_or_serializes_private_data(self):
        d = self.dash
        for platform, mode in [('linux', 'developer'), ('linux', 'gaming'),
                               ('container', 'developer'), ('macos', 'developer')]:
            with self.subTest(platform=platform, mode=mode), ExitStack() as stack:
                stack.enter_context(mock.patch.object(d, 'MACOS', platform == 'macos'))
                stack.enter_context(mock.patch.object(d, 'PORTABLE', platform == 'container'))
                for name in PRIVATE_COLLECTORS:
                    stack.enter_context(mock.patch.object(d, name, side_effect=AssertionError(f'private collector: {name}')))
                stack.enter_context(mock.patch.object(d, 'APPS', [{'url': 'https://PRIVATE-SENTINEL.example'}]))
                stack.enter_context(mock.patch.object(d, 'OS_IDENTITY', {'private': 'PRIVATE-SENTINEL'}))
                system = {'uptime_s': 42, 'cpu': {'pct': 12, 'model': 'PRIVATE-SENTINEL'},
                          'mem': {'pct': 34}, 'disks': [{'pct': 56, 'label': 'PRIVATE-SENTINEL'}],
                          'temps': {'cpu': 60, 'gpu': 70, 'fans': ['PRIVATE-SENTINEL']},
                          'new_private_field': 'PRIVATE-SENTINEL'}
                stack.enter_context(mock.patch.object(d, 'collect_system',
                    **({'side_effect': AssertionError('portable host read')} if platform == 'container' else {'return_value': system})))
                unit = stack.enter_context(mock.patch.object(d, 'unit_state', return_value={'active': 'active' if mode == 'developer' else 'inactive'}))
                server = d.ThreadingHTTPServer(('127.0.0.1', 0), d.Handler)
                thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
                try:
                    for headers in ({}, {'Tailscale-User-Login': 'denied@example.test'},
                                    {'X-Grave-Local-Token': 'f' * 64}):
                        for path in ('/api/state', '/'):
                            req = urllib.request.Request(f'http://127.0.0.1:{server.server_port}{path}', headers=headers)
                            with urllib.request.urlopen(req, timeout=5) as response:
                                body = response.read().decode()
                            self.assertNotIn('PRIVATE-SENTINEL', body)
                            if path == '/':
                                body = body.split('const BOOT=', 1)[1].split(';   // server-rendered', 1)[0]
                            state = json.loads(body)
                            d.validate_public_state(state)
                            self.assertEqual(state['platform'], platform)
                            self.assertEqual(state['mode'], mode)
                            self.assertEqual(state['resources']['cpu_pct'], None if platform == 'container' else 12)
                    if platform != 'linux':
                        unit.assert_not_called()
                finally:
                    server.shutdown(); server.server_close(); thread.join(timeout=5)

    def test_public_resource_values_are_numeric_and_doctor_rejects_drift(self):
        d = self.dash
        with mock.patch.object(d, 'MACOS', True), mock.patch.object(d, 'collect_system', return_value={
                'uptime_s': 'private', 'cpu': {'pct': float('nan')}, 'mem': {'pct': True},
                'temps': {'cpu': 'private', 'gpu': float('inf')}}):
            result = d.state({})
        self.assertTrue(all(value is None for value in result['resources'].values()))
        d.validate_public_state(result)
        for payload in ({**result, 'settings': {}}, {**result, 'resources': {'cpu_pct': 'private'}}):
            with self.assertRaises(AssertionError):
                d.validate_public_state(payload)
