"""Live HTTP contracts for direct, owner-authorized management clients."""
import http.client
import json
import pathlib
import tempfile
import threading
import unittest
from unittest import mock

import test_dashboard as fixtures


class ClientAPI(unittest.TestCase):
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
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()
        cls.root.cleanup()

    def setUp(self):
        self.origin = 'https://home.tail123.ts.net'
        self.d.save_client_origins({'origins': [self.origin]})

    def request(self, path, method='GET', data=None, headers=None):
        connection = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=5)
        request_headers = {'Tailscale-User-Login': 'owner@example.test', 'X-Grave-Client': '1',
                           'Origin': self.origin, 'Sec-Fetch-Site': 'same-site'}
        request_headers.update(headers or {})
        request_headers = {k: v for k, v in request_headers.items() if v is not None}
        body = json.dumps(data) if data is not None else None
        if body is not None:
            request_headers['Content-Type'] = 'application/json'
        try:
            connection.request(method, path, body, request_headers)
            response = connection.getresponse()
            payload = response.read()
            return response.status, dict(response.getheaders()), json.loads(payload) if payload else None
        finally:
            connection.close()

    def test_trusted_owner_reads_private_state_with_exact_cors(self):
        with mock.patch.object(self.d, 'state', return_value={'host': 'destination', 'settings': {'poll_ms': 5000}}):
            code, headers, value = self.request('/grave/api/v1/state')
        self.assertEqual(code, 200)
        self.assertEqual(value['host'], 'destination')
        self.assertEqual(headers['Access-Control-Allow-Origin'], self.origin)
        self.assertEqual(headers['Cache-Control'], 'no-store')
        self.assertEqual(headers['Vary'], 'Origin')
        self.assertNotIn('Access-Control-Allow-Credentials', headers)

    def test_untrusted_origin_and_revoked_origin_never_collect(self):
        for origins in ([], ['https://other.tail123.ts.net']):
            self.d.save_client_origins({'origins': origins})
            with mock.patch.object(self.d, 'state') as collect:
                code, headers, value = self.request('/api/v1/state')
            self.assertEqual(code, 403)
            self.assertEqual(value['error'], 'untrusted_origin')
            self.assertNotIn('Access-Control-Allow-Origin', headers)
            collect.assert_not_called()

    def test_trusted_origin_does_not_authorize_viewer(self):
        for login in (None, 'outsider@example.test'):
            with mock.patch.object(self.d, 'state') as collect:
                code, _, value = self.request('/api/v1/state', headers={'Tailscale-User-Login': login})
            self.assertEqual((code, value['error']), (403, 'forbidden'))
            collect.assert_not_called()

    def test_preflight_only_allows_supported_methods_and_headers(self):
        headers = {'Access-Control-Request-Method': 'POST', 'Access-Control-Request-Headers': 'content-type,x-grave-client', 'X-Grave-Client': None}
        code, response, _ = self.request('/api/v1/settings', 'OPTIONS', headers=headers)
        self.assertEqual(code, 204)
        self.assertEqual(response['Access-Control-Allow-Methods'], 'POST')
        for extra in ({'Access-Control-Request-Method': 'DELETE'},
                      {'Access-Control-Request-Headers': 'x-grave-local-token'},
                      {'Origin': 'https://evil.example'}):
            code, _, _ = self.request('/api/v1/settings', 'OPTIONS', headers={**headers, **extra})
            self.assertIn(code, (403, 404))
        self.assertEqual(self.request('/api/settings', 'OPTIONS', headers=headers)[0], 403)

    def test_mutation_reuses_settings_handler(self):
        with mock.patch.object(self.d, 'settings_response', side_effect=lambda s: {'ok': True, 'settings': s}):
            code, _, data = self.request('/api/v1/settings', 'POST', {'poll_ms': 7000})
        self.assertEqual(code, 200)
        self.assertEqual(data['settings']['poll_ms'], 7000)
        self.assertEqual(self.d.load_settings()['poll_ms'], 7000)

    def test_benchmark_operation_validates_payload_and_runs_on_destination(self):
        with mock.patch.object(self.d, 'benchmark', return_value=(200, {'state': 'running'})) as run:
            code, _, result = self.request('/api/v1/admin/benchmark', 'POST', {'action': 'start', 'mode': 'quick'})
            self.assertEqual((code, result['state']), (200, 'running'))
            run.assert_called_once_with('start', 'quick')
            run.reset_mock()
            self.assertEqual(self.request('/api/v1/admin/benchmark', 'POST', {'action': 'start', 'mode': 'unknown'})[0], 400)
            run.assert_not_called()

    def test_form_cannot_mutate_and_legacy_gate_remains_closed(self):
        with mock.patch.object(self.d, 'save_settings') as save:
            for path, headers in (('/api/v1/settings', {'X-Grave-Client': None}),
                                  ('/api/settings', {})):
                self.assertEqual(self.request(path, 'POST', {'poll_ms': 7000}, headers)[0], 403)
        save.assert_not_called()

    def test_native_owner_requires_protocol_header_but_not_origin(self):
        headers = {'Origin': None, 'Sec-Fetch-Site': None}
        code, response, data = self.request('/api/v1/capabilities', headers=headers)
        self.assertEqual(code, 200)
        self.assertEqual(data['api_version'], 1)
        self.assertNotIn('Access-Control-Allow-Origin', response)
        self.assertNotIn('action-stream', data['routes']['GET'])
        self.assertIn('action-stream', data['routes']['POST'])
        self.assertNotIn('client-trust', data['routes']['POST'])

    def test_streams_are_post_only_and_still_authorized(self):
        with mock.patch.object(self.d.Handler, '_stream_action') as run:
            self.assertEqual(self.request('/api/v1/action-stream?action=reboot')[0], 404)
            self.assertEqual(self.request('/api/v1/action-stream?action=reboot', 'POST', headers={'Origin': 'https://evil.example'})[0], 403)
        run.assert_not_called()
        code, _, data = self.request('/api/v1/action-stream?action=not-an-action', 'POST')
        self.assertEqual(code, 400)
        self.assertEqual(data['output'], 'unknown action')

    def test_unsupported_deployments_fail_closed(self):
        for name, value in [('PORTABLE', True), ('BACKEND_TOKEN', 'x' * 32), ('REQUIRE_BACKEND_TOKEN', True)]:
            with mock.patch.object(self.d, name, value):
                self.assertIn(self.request('/api/v1/capabilities')[0], (401, 501))

    def test_mac_capabilities_match_available_routes(self):
        with mock.patch.object(self.d, 'MACOS', True), mock.patch.object(self.d, 'MACOS_AGENTS', False):
            routes = self.d.client_routes()
            self.assertNotIn('fs', routes['POST'])
            self.assertNotIn('action-stream', routes['POST'])
            self.assertIn('settings', routes['POST'])

    def test_trust_changes_require_local_origin_and_never_alias(self):
        data = {'origins': ['https://other.tail123.ts.net']}
        self.assertEqual(self.request('/api/client-trust', 'POST', data)[0], 403)
        self.assertEqual(self.request('/api/v1/client-trust', 'POST', data)[0], 404)
        code, _, _ = self.request('/api/client-trust', 'POST', data,
                                 {'Origin': None, 'Sec-Fetch-Site': None})
        self.assertEqual(code, 200)
        self.assertEqual(self.d.client_origins(), data['origins'])
        self.assertEqual(pathlib.Path(self.d.CLIENT_TRUST_PATH).stat().st_mode & 0o777, 0o600)

    def test_trust_validation_and_file_boundary(self):
        for value in ['*', 'null', 'https://evil.example', self.origin+'/', self.origin+':443',
                      'https://user@home.tail123.ts.net', 'http://home.tail123.ts.net']:
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.d.save_client_origins({'origins': [value]})
        self.assertIsNone(self.d._safe_path('config/secrets/dashboard-trusted-origins.json'))
        for op in ['delete', 'rename']:
            self.assertFalse(self.d.fs_op({'op': op, 'path': 'config', 'name': 'moved'})['ok'])
        self.assertTrue(pathlib.Path(self.d.CLIENT_TRUST_PATH).exists())

    def test_public_summary_remains_without_cors(self):
        with mock.patch.object(self.d, 'summary', return_value={'product': 'gravedecay'}):
            code, headers, _ = self.request('/api/v1/summary', headers={'Tailscale-User-Login': None})
        self.assertEqual(code, 200)
        self.assertNotIn('Access-Control-Allow-Origin', headers)

    def test_unknown_versions_do_not_reach_legacy_handlers(self):
        with mock.patch.object(self.d, 'state') as collect:
            self.assertEqual(self.request('/api/v2/state')[0], 404)
        collect.assert_not_called()
