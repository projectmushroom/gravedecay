"""Execution, persistence and authorization contracts for resumable actions."""
import concurrent.futures
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import uuid
from unittest import mock

import test_dashboard as fixtures
import test_client_api as client_fixtures

ops = fixtures.DASHBOARD.operations


def new_id(age=0):
    return str(int(time.time()) - age) + '-' + uuid.uuid4().hex


class Operations(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.lock = ops.ActionLock(self.root / 'action.lock')
        self.store = ops.Store(self.root, self.lock, timeout=2)

    def tearDown(self):
        # No worker is allowed to escape the fixture that owns its journal.
        deadline = time.monotonic() + 4
        while self.lock.mutex.locked() and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertFalse(self.lock.mutex.locked())
        self.temp.cleanup()

    def finish(self, operation_id):
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline:
            result = self.store.get(operation_id)
            if result['state'] not in ops.ACTIVE and not self.lock.mutex.locked():
                return result
            time.sleep(.01)
        self.fail('operation did not finish')

    def test_concurrent_retries_execute_once_and_resume_output(self):
        operation_id = new_id()
        marker = self.root / 'count'
        cmd = [sys.executable, '-c', f"from pathlib import Path; import time; p=Path({str(marker)!r}); p.write_text(p.read_text()+'x' if p.exists() else 'x'); print('hello',flush=True); time.sleep(.15); print('bye')"]
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            replies = list(pool.map(lambda _: self.store.start(operation_id, 'doctor', cmd), range(12)))
        self.assertEqual(sum(created for _, created in replies), 1)
        result = self.finish(operation_id)
        self.assertEqual(marker.read_text(), 'x')
        self.assertEqual(result['state'], 'succeeded')
        self.assertEqual(''.join(e['text'] for e in result['events']), 'hello\nbye\n')
        self.assertEqual(self.store.get(operation_id, result['cursor'])['events'], [])
        after = result['events'][0]['seq']
        self.assertTrue(all(e['seq'] > after for e in self.store.get(operation_id, after)['events']))
        self.assertNotIn('instance', result)
        self.assertEqual((self.root / (operation_id+'.json')).stat().st_mode & 0o777, 0o600)

    def test_conflict_and_legacy_action_exclusion(self):
        operation_id = new_id()
        self.store.start(operation_id, 'doctor', [sys.executable, '-c', 'import time; time.sleep(.15)'])
        with self.assertRaises(ops.OperationError) as error:
            self.store.start(operation_id, 'reboot', ['never-run'])
        self.assertEqual(error.exception.code, 'id_conflict')
        self.assertFalse(self.lock.acquire())
        other = ops.Store(self.root, ops.ActionLock(self.root / 'action.lock'))
        with self.assertRaises(ops.OperationError) as error:
            other.start(new_id(), 'doctor', ['never-run'])
        self.assertEqual(error.exception.code, 'busy')
        self.finish(operation_id)
        self.assertTrue(self.lock.acquire())
        try:
            with self.assertRaises(ops.OperationError):
                self.store.start(new_id(), 'doctor', ['never-run'])
        finally:
            self.lock.release()

    def test_silent_timeout_kills_descendants_and_unlocks(self):
        self.store.timeout = .2
        operation_id = new_id()
        result, _ = self.store.start(operation_id, 'doctor', [sys.executable, '-c',
            'import subprocess,sys; subprocess.Popen([sys.executable,"-c","import time; time.sleep(30)"]); import time; time.sleep(30)'])
        self.assertEqual(self.finish(operation_id)['state'], 'timed_out')
        operation_id = new_id()
        self.store.start(operation_id, 'doctor', [sys.executable, '-c', 'print("next")'])
        self.assertEqual(self.finish(operation_id)['state'], 'succeeded')

    def test_missing_executable_and_nonzero_exit_are_final(self):
        for cmd in (['/no/such/grave-command'], [sys.executable, '-c', 'raise SystemExit(7)']):
            operation_id = new_id()
            self.store.start(operation_id, 'doctor', cmd)
            self.assertEqual(self.finish(operation_id)['state'], 'failed')
            _, created = self.store.start(operation_id, 'doctor', cmd)
            self.assertFalse(created)

    def test_output_bound_and_multibyte_chunks(self):
        operation_id = new_id()
        self.store.start(operation_id, 'doctor', [sys.executable, '-c', "import sys; sys.stdout.buffer.write(('💀'*100000).encode())"])
        result = self.finish(operation_id)
        text = ''.join(e['text'] for e in result['events'])
        self.assertNotIn('\ufffd', text)
        self.assertLessEqual(len(text.encode()), ops.MAX_OUTPUT)
        self.assertTrue(result['truncated'])
        self.assertTrue(self.store.health()['ok'])

    def test_restart_reports_unknown_without_replay(self):
        operation_id = new_id()
        self.store.start(operation_id, 'doctor', [sys.executable, '-c', 'print("once")'])
        self.finish(operation_id)
        path = self.root / (operation_id+'.json')
        record = json.loads(path.read_text())
        record['state'] = 'running'
        path.write_text(json.dumps(record))
        restarted = ops.Store(self.root, self.lock)
        with mock.patch.object(ops.subprocess, 'Popen') as spawn:
            result, created = restarted.start(operation_id, 'doctor', ['never-run'])
        self.assertFalse(created)
        self.assertEqual(result['state'], 'interrupted')
        spawn.assert_not_called()
        self.assertIn('outcome unknown', result['message'])

    def test_crashed_dashboard_cannot_overlap_surviving_command(self):
        operation_id = new_id()
        marker = self.root / 'started'
        done = self.root / 'done'
        child = [sys.executable, '-c', f'from pathlib import Path; import time; Path({str(marker)!r}).touch(); time.sleep(.6); Path({str(done)!r}).touch()']
        script = f'''import sys,time
sys.path.insert(0,{str(fixtures.ROOT / 'dashboard')!r})
import operations
store=operations.Store({str(self.root)!r},operations.ActionLock({str(self.root / 'action.lock')!r}))
store.start({operation_id!r},'doctor',{child!r})
time.sleep(30)
'''
        server = subprocess.Popen([sys.executable, '-c', script])
        try:
            deadline = time.monotonic() + 3
            while not marker.exists() and time.monotonic() < deadline:
                time.sleep(.01)
            self.assertTrue(marker.exists())
            server.kill(); server.wait()
            self.assertEqual(self.store.get(operation_id)['state'], 'interrupted')
            with self.assertRaises(ops.OperationError) as error:
                self.store.start(new_id(), 'doctor', ['never-run'])
            self.assertEqual(error.exception.code, 'busy')
            while not done.exists() and time.monotonic() < deadline:
                time.sleep(.01)
            self.assertTrue(done.exists())
        finally:
            if server.poll() is None:
                server.kill()
            server.wait()

    def test_retention_expired_ids_capacity_and_validation(self):
        for value in ('../../etc/passwd', '', None, 42):
            with self.assertRaises(ops.OperationError):
                self.store.get(value)
        old_id = new_id(ops.RETENTION + 10)
        with self.assertRaises(ops.OperationError) as error:
            self.store.start(old_id, 'doctor', ['never-run'])
        self.assertEqual(error.exception.code, 'expired_id')
        with mock.patch.object(ops, 'MAX_RECORDS', 0):
            with self.assertRaises(ops.OperationError) as error:
                self.store.start(new_id(), 'doctor', ['never-run'])
        self.assertEqual(error.exception.code, 'history_full')
        # Expired history is pruned only during a new start, while no job runs.
        (self.root / (old_id+'.json')).write_text('{}')
        current = new_id()
        self.store.start(current, 'doctor', [sys.executable, '-c', 'pass'])
        self.finish(current)
        self.assertFalse((self.root / (old_id+'.json')).exists())
        with self.assertRaises(ops.OperationError):
            self.store.start(old_id, 'doctor', ['never-run'])

    def test_corrupt_record_never_becomes_a_fresh_submission(self):
        operation_id = new_id()
        (self.root / (operation_id+'.json')).write_text('{}')
        with mock.patch.object(ops.subprocess, 'Popen') as spawn:
            with self.assertRaises(OSError):
                self.store.start(operation_id, 'doctor', ['never-run'])
            with self.assertRaises(OSError):
                self.store.get(operation_id)
        spawn.assert_not_called()

    def test_failed_journal_write_never_starts_command_or_leaks_lock(self):
        with mock.patch.object(self.store, '_write', side_effect=OSError('full')), mock.patch.object(ops.subprocess, 'Popen') as spawn:
            with self.assertRaises(OSError):
                self.store.start(new_id(), 'doctor', ['never-run'])
        spawn.assert_not_called()
        self.assertTrue(self.lock.acquire()); self.lock.release()


class OperationHTTP(unittest.TestCase):
    request = client_fixtures.ClientAPI.request

    @classmethod
    def setUpClass(cls):
        cls.root = tempfile.TemporaryDirectory()
        cls.d = fixtures.load_dashboard({'GRAVE_ROOT': cls.root.name, 'GRAVEDECAY_PLATFORM': 'linux',
                                        'GRAVEDECAY_ALLOWED_USERS': 'owner@example.test'})
        cls.server = cls.d.ThreadingHTTPServer(('127.0.0.1', 0), cls.d.Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown(); cls.server.server_close(); cls.thread.join()
        cls.root.cleanup()

    def setUp(self):
        self.origin = 'https://home.tail123.ts.net'
        self.d.save_client_origins({'origins': [self.origin]})

    def test_owner_launch_read_retry_and_revocation(self):
        operation_id = new_id()
        data = {'id': operation_id, 'action': 'doctor'}
        with mock.patch.dict(self.d.ACTIONS, {'doctor': [sys.executable, '-c', 'print("hello")']}):
            code, _, result = self.request('/api/v1/operations', 'POST', data)
            self.assertEqual(code, 202)
            self.assertEqual(result['id'], operation_id)
            self.assertEqual(self.request('/api/v1/operations', 'POST', data)[0], 200)
        deadline = time.monotonic() + 3
        while self.d.ACTION_LOCK.mutex.locked() and time.monotonic() < deadline:
            time.sleep(.01)
        code, headers, result = self.request('/api/v1/operations?id='+operation_id)
        self.assertEqual(code, 200)
        self.assertEqual(result['state'], 'succeeded')
        self.assertEqual(headers['Access-Control-Allow-Origin'], self.origin)
        self.d.save_client_origins({'origins': []})
        self.assertEqual(self.request('/api/v1/operations?id='+operation_id)[0], 403)

    def test_viewers_legacy_cross_site_and_invalid_actions_never_start(self):
        with mock.patch.object(self.d.OPERATIONS, 'start') as start:
            data = {'id': new_id(), 'action': 'doctor'}
            for path, headers in [('/api/v1/operations', {'Tailscale-User-Login': None}),
                                  ('/api/v1/operations', {'X-Grave-Client': None}),
                                  ('/api/operations', {})]:
                self.assertEqual(self.request(path, 'POST', data, headers)[0], 403)
                self.assertEqual(self.request(path+'?id='+data['id'], headers=headers)[0], 403)
            for action in ('t3-pair', 'update-grave', 'arbitrary-shell', [], None):
                self.assertEqual(self.request('/api/v1/operations', 'POST', {**data, 'action': action})[0], 400)
            self.assertEqual(self.request('/api/v1/operations', 'POST', {**data, 'args': ['--evil']})[0], 400)
            self.assertEqual(self.request('/api/v1/operations?id='+data['id']+'&after=-1')[0], 400)
        start.assert_not_called()

    def test_doctor_probe_and_platform_capabilities(self):
        self.assertEqual(self.request('/api/v1/operations?health=1')[2]['protocol'], 1)
        caps = self.request('/api/v1/capabilities')[2]
        self.assertIn('doctor', caps['operations']['actions'])
        self.assertNotIn('t3-pair', caps['operations']['actions'])
        for name in ('PORTABLE', 'REQUIRE_BACKEND_TOKEN', 'MACOS'):
            with mock.patch.object(self.d, name, True):
                self.assertIn(self.request('/api/v1/operations?health=1')[0], (401, 404, 501))
                self.assertIn(self.request('/api/operations?health=1')[0], (401, 501))
