"""Live resource responses, schema drift, access gates and revisioned mutations."""
import concurrent.futures
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock

from jsonschema import Draft202012Validator, FormatChecker
from openapi_spec_validator import validate
import test_dashboard as fixtures
import test_client_api as clients


class Resources(unittest.TestCase):
    request = clients.ClientAPI.request

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
        cls.server.shutdown(); cls.server.server_close(); cls.thread.join(); cls.root.cleanup()

    def setUp(self):
        self.origin = 'https://home.tail123.ts.net'
        self.d.save_client_origins({'origins': [self.origin]})
        self.d._RESOURCE_CACHE.clear()
        self.d.save_settings(self.d.DEFAULT_SETTINGS)

    def schema(self, name, value):
        Draft202012Validator({'$ref': '#/components/schemas/'+name,
                              'components': self.d.API_DOCUMENT['components']},
                             format_checker=FormatChecker()).validate(value)

    def resource(self, name):
        code, headers, value = self.request('/grave/api/v1/resources/'+name)
        self.assertEqual(code, 200, value)
        self.schema(name.title()+'Resource', value)
        self.assertEqual(headers['Access-Control-Allow-Origin'], self.origin)
        self.assertEqual(headers['Cache-Control'], 'no-store')
        return value

    def test_schema_is_valid_complete_deterministic_and_matches_capabilities(self):
        spec = self.request('/api/v1/openapi.json')[2]
        validate(spec)
        caps = self.request('/api/v1/capabilities')[2]
        self.schema('Capabilities', caps)
        self.assertEqual(caps['resource_contract']['sha256'], self.d.api_contract.digest(spec))
        for method, names in self.d.CLIENT_ROUTES.items():
            for name in names:
                self.assertIn(method.lower(), spec['paths']['/'+name])
        snapshot = json.loads((fixtures.ROOT/'docs/openapi.json').read_text())
        self.assertEqual(snapshot, self.d.api_contract.document(self.d.CLIENT_ROUTES))
        self.assertTrue(spec['x-grave-authorization']['required'])
        for methods in spec['paths'].values():
            for operation in methods.values():
                self.assertNotIn('Tailscale-User-Login', [p['name'] for p in operation['parameters']])
        result = subprocess.run([sys.executable, str(fixtures.ROOT/'dashboard/api_contract.py'),
                                 '--check', str(fixtures.ROOT/'docs/openapi.json')], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_system_read_is_independent_numeric_and_does_not_leak_collector_fields(self):
        sample = {'uptime_s': 12, 'ncpu': 8, 'load': [0, float('nan'), 2.1],
                  'cpu': {'pct': 23, 'secret': 'never-return'},
                  'mem': {'total_kb': 4096, 'used_kb': 1024, 'pct': 25},
                  'disks': [{'label': '/', 'total': 1024, 'used': 512, 'pct': 50}],
                  'temps': {'cpu': float('inf'), 'gpu': True}, 'secret': 'never-return'}
        with mock.patch.object(self.d, 'collect_system', return_value=sample) as collect, \
             mock.patch.object(self.d, '_state', side_effect=AssertionError('dashboard collected')), \
             mock.patch.object(self.d, 'collect_github', side_effect=AssertionError('remote API called')):
            value = self.resource('system')
            self.resource('system')
        collect.assert_called_once()
        self.assertEqual(value['data']['memory']['used_bytes'], 1048576)
        self.assertEqual(value['data']['temperature'], {'cpu_celsius': None, 'gpu_celsius': None})
        self.assertIsNone(value['data']['load_average'][1])
        self.assertNotIn('never-return', json.dumps(value))
        with mock.patch.object(self.d, 'MACOS', True), mock.patch.object(self.d, 'collect_system', return_value=sample):
            self.d._RESOURCE_CACHE.clear()
            mac = self.resource('system')['data']['memory']
        self.assertEqual(mac['percent_kind'], 'pressure')
        self.assertIsNone(mac['used_bytes'])

    def test_collection_shapes_and_typed_unknowns(self):
        cases = [
            ('services', 'collect_services', [{'unit':'t3code', 'active':'active', 'sub':'running', 'secret':'hidden'}]),
            ('containers', 'collect_docker', {'error':None,'containers':[{'name':'redis','state':'running','status':'Up','project':'core','secret':'hidden'}]}),
            ('sessions', 'collect_tmux', [{'name':'coding','windows':'2','attached':'detached','activity':'today','secret':'hidden'}]),
            ('repositories', 'collect_repos', [{'name':'repo','branch':'main','dirty':3,'last_subject':'message','secret':'hidden'}]),
        ]
        with mock.patch.object(self.d, 'resource_paused', return_value=False):
            for name, collector, rows in cases:
                with mock.patch.object(self.d, collector, return_value=rows):
                    value = self.resource(name)
                self.assertEqual(value['status'], 'ready')
                self.assertNotIn('hidden', json.dumps(value))
                self.assertEqual(len(value['data']), 1)
                if name == 'sessions':
                    self.assertEqual(value['data'][0]['window_count'], 2)
                    self.assertIs(value['data'][0]['attached'], False)

    def test_failed_paused_empty_and_truncated_collections_are_distinct(self):
        with mock.patch.object(self.d, 'collect_system', side_effect=OSError('private details')):
            result = self.resource('system')
        self.assertEqual(result['status'], 'unavailable')
        self.assertIsNone(result['data'])
        self.assertNotIn('private details', json.dumps(result))
        with mock.patch.object(self.d, 'resource_paused', return_value=True), mock.patch.object(self.d, 'collect_repos') as collect:
            self.assertEqual(self.resource('repositories')['status'], 'paused')
            collect.assert_not_called()
        with mock.patch.object(self.d, 'collect_services', return_value=[]):
            self.assertEqual(self.resource('services')['data'], [])
        self.d._RESOURCE_CACHE.clear()
        rows = [{'unit':str(i), 'active':'active','sub':'running'} for i in range(501)]
        with mock.patch.object(self.d, 'collect_services', return_value=rows):
            result = self.resource('services')
        self.assertEqual((result['status'], result['truncated'], len(result['data'])), ('partial', True, 500))

    def test_mac_capabilities_and_partial_repository_scan(self):
        with mock.patch.object(self.d, 'MACOS', True), mock.patch.object(self.d, 'MACOS_AGENTS', False):
            caps = self.request('/api/v1/capabilities')[2]
            self.assertNotIn('sessions', caps['resource_contract']['resources'])
            self.assertEqual(self.request('/api/v1/resources/sessions')[0], 404)
            with mock.patch.object(self.d, 'collect_macos_repo_inventory', return_value={
                    'repos':[{'name':'owner/repo', 'branch':'main','dirty':0}], 'error':'private path', 'truncated':True}):
                value = self.resource('repositories')
            self.assertEqual(value['status'], 'partial')
            self.assertTrue(value['truncated'])
            self.assertNotIn('private path', json.dumps(value))
            self.resource('preferences')

    def test_every_resource_and_schema_requires_owner_and_trusted_origin(self):
        names = ['resources/'+n for n in self.d.api_contract.RESOURCE_NAMES] + ['openapi.json']
        with mock.patch.object(self.d, 'resource_read') as collect:
            for name in names:
                for headers in ({'Tailscale-User-Login':None}, {'Origin':'https://evil.example'}, {'X-Grave-Client':None}):
                    code, _, _ = self.request('/api/v1/'+name, headers=headers)
                    self.assertEqual(code, 403, name)
                self.assertEqual(self.request('/api/'+name, headers={'Origin':None,'Sec-Fetch-Site':None})[0], 404)
            collect.assert_not_called()
        self.d.save_client_origins({'origins':[]})
        self.assertEqual(self.request('/api/v1/resources/preferences')[0], 403)
        for flag in ('PORTABLE', 'REQUIRE_BACKEND_TOKEN'):
            with mock.patch.object(self.d, flag, True):
                self.assertIn(self.request('/api/v1/resources/system')[0], (401,501))

    def test_missing_tools_are_unavailable_and_preferences_reject_unauthorized_writes(self):
        with mock.patch.object(self.d, 'resource_paused', return_value=False), mock.patch.object(self.d.shutil, 'which', return_value=None):
            for name in ('sessions', 'repositories'):
                self.assertEqual(self.resource(name)['status'], 'unavailable')
        body = {'revision': self.resource('preferences')['data']['revision'], 'changes': {'poll_ms':7000}}
        with mock.patch.object(self.d, 'patch_preferences') as patch:
            for headers in ({'Tailscale-User-Login':None}, {'Origin':'https://evil.example'}, {'X-Grave-Client':None}):
                code, _, error = self.request('/api/v1/resources/preferences', 'POST', body, headers)
                self.assertEqual(code,403)
                self.schema('Error',error)
            self.assertEqual(self.request('/api/v1/resources/system', 'POST', body)[0],404)
            patch.assert_not_called()
        code, _, _ = self.request('/api/v1/resources/preferences','OPTIONS',headers={
            'Access-Control-Request-Method':'POST','Access-Control-Request-Headers':'Content-Type, X-Grave-Client'})
        self.assertEqual(code,204)

    def test_preference_patch_is_strict_atomic_and_preserves_unrelated_settings(self):
        before = self.resource('preferences')['data']
        body = {'revision': before['revision'], 'changes': {'poll_ms':7000, 'hidden_panels':['services']}}
        code, _, result = self.request('/api/v1/resources/preferences', 'POST', body)
        self.assertEqual(code, 200)
        self.schema('PreferencesResource', result)
        self.assertEqual(result['data']['values']['poll_ms'], 7000)
        self.assertEqual(result['data']['values']['custom_apps'], before['values']['custom_apps'])
        self.assertNotEqual(result['data']['revision'], before['revision'])
        self.assertEqual(Path(self.d.SETTINGS_PATH).stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.request('/api/v1/resources/preferences', 'POST', body)[0], 409)
        invalid = [None, [], {'poll_ms':True}, {'poll_ms':1999}, {'poll_ms':float('nan')}, {'linear_key':'secret'},
                   {'hidden_panels':['x','x']}, {'hidden_apps':[{}]}, {'t3_tile':'bad'},
                   {'custom_apps':[{'name':'evil','url':'javascript:alert(1)'}]},
                   {'custom_apps':[{'name':'evil','url':'//evil.example'}]},
                   {'custom_apps':[{'name':'evil','url':'https://user:password@host.example'}]}]
        for changes in invalid:
            code, _, error = self.request('/api/v1/resources/preferences', 'POST', {'revision':result['data']['revision'],'changes':changes})
            self.assertEqual(code, 400, changes)
            self.schema('Error', error)
        self.assertEqual(self.resource('preferences')['data'], result['data'])

    def test_concurrent_writers_and_legacy_edits_cannot_silently_overwrite(self):
        revision = self.resource('preferences')['data']['revision']
        def write(ms):
            return self.request('/api/v1/resources/preferences', 'POST', {'revision':revision,'changes':{'poll_ms':ms}})[0]
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            self.assertEqual(sorted(pool.map(write, [6000,8000])), [200,409])
        revision = self.resource('preferences')['data']['revision']
        self.assertEqual(self.request('/api/v1/settings','POST',{'poll_ms':9000})[0],200)
        self.assertEqual(write(10000),409)
        self.assertEqual(self.resource('preferences')['data']['values']['poll_ms'],9000)

    def test_corrupt_settings_and_failed_write_preserve_existing_data(self):
        before = self.resource('preferences')['data']
        path=Path(self.d.SETTINGS_PATH)
        original=path.read_bytes()
        with mock.patch.object(self.d.os,'replace',side_effect=OSError('disk full')):
            self.assertEqual(self.request('/api/v1/resources/preferences','POST',{
                'revision':before['revision'],'changes':{'poll_ms':7000}})[0],503)
        self.assertEqual(path.read_bytes(),original)
        path.write_text('{ broken')
        self.assertEqual(self.request('/api/v1/resources/preferences')[0],503)
        self.assertEqual(self.request('/api/v1/resources/preferences','POST',{
            'revision':before['revision'],'changes':{'poll_ms':7000}})[0],503)
        self.assertEqual(path.read_text(),'{ broken')
        path.write_bytes(original)

    def test_new_resource_does_not_expose_legacy_credentials_or_integration_settings(self):
        self.d.save_settings({'custom_apps':[{'name':'private','url':'https://user:password@host.example'},
                                            {'name':'safe','url':'/term/'}]})
        value = self.resource('preferences')
        self.assertEqual(value['data']['values']['custom_apps'],[{'name':'safe','url':'/term/'}])
        self.assertNotIn('password', json.dumps(value))
        self.assertNotIn('repo_root',value['data']['values'])
