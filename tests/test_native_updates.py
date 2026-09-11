import concurrent.futures
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('native_updates', Path(__file__).parents[1] / 'dashboard/native_updates.py')
updates = importlib.util.module_from_spec(spec)
spec.loader.exec_module(updates)


class NativeUpdateMailboxTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(patch.stopall)
        patch.dict(os.environ, GRAVE_ROOT=self.temp.name).start()
        self.directory = Path(self.temp.name) / 'updates'
        self.directory.mkdir()
        self.status = self.directory / 'status.json'
        self.status.write_text(json.dumps({'state': 'idle', 'current': 'v0.28.0',
            'latest': 'v0.29.0', 'available': True, 'releases': ['v0.29.0']}))

    def test_only_one_request_wins_and_reports_the_same_attempt(self):
        with concurrent.futures.ThreadPoolExecutor(2) as executor:
            results = list(executor.map(lambda _: updates.request({'tag': 'v0.29.0'}), range(2)))
        self.assertEqual(sorted(code for code, _ in results), [202, 409])
        request = self.directory / 'request.json'
        payload = json.loads(request.read_text())
        self.assertEqual(request.stat().st_mode & 0o777, 0o600)
        self.assertEqual(updates.status()['attempt'], payload['attempt'])
        self.assertEqual(updates.status()['state'], 'queued')
        self.assertEqual(payload['current'], 'v0.28.0')

    def test_unknown_release_urls_commands_and_stale_app_are_rejected(self):
        for data in ({'tag': 'v0.27.0'}, {'tag': 'v9.9.9'}, {'tag': '../../evil'},
                     {'url': 'https://example.com/app.zip'}, {'command': 'anything'}, {'tag': []}):
            self.assertIn(updates.request(data)[0], (400, 409))
        self.assertFalse((self.directory / 'request.json').exists())
        os.utime(self.status, (time.time() - 60,) * 2)
        self.assertEqual(updates.request({'tag': 'v0.29.0'})[0], 503)
        self.assertFalse(updates.status()['available'])

    def test_request_is_hidden_until_its_json_is_complete(self):
        def write_in_parts(payload, stream):
            stream.write('{')
            stream.flush()
            self.assertFalse((self.directory / 'request.json').exists())
            self.assertEqual(updates.status()['state'], 'idle')
            stream.write(json.dumps(payload)[1:])

        with patch.object(updates.json, 'dump', side_effect=write_in_parts):
            self.assertEqual(updates.request({'tag': 'v0.29.0'})[0], 202)
        self.assertEqual(updates.status()['state'], 'queued')
        self.assertEqual(sorted(p.name for p in self.directory.iterdir()), ['request.json', 'status.json'])

    def test_explicit_check_has_no_install_target(self):
        self.assertEqual(updates.request({'tag': 'v0.29.0'}, check=True)[0], 400)
        self.assertEqual(updates.request({}, check=True)[0], 202)
        payload = json.loads((self.directory / 'request.json').read_text())
        self.assertEqual(payload['action'], 'check')
        self.assertEqual(payload['tag'], '')

    def test_success_requires_the_requested_app_version(self):
        self.status.write_text(json.dumps({'state': 'ok', 'current': 'v0.28.0', 'target': 'v0.29.0'}))
        self.assertFalse(updates.status()['dashboard_current'])
        self.status.write_text(json.dumps({'state': 'ok', 'current': 'v0.29.0', 'target': 'v0.29.0'}))
        self.assertTrue(updates.status()['dashboard_current'])
