"""Structured client representations and the OpenAPI contract (stdlib only)."""
import copy
import hashlib
import json
import math
import re
import sys
import urllib.parse
from pathlib import Path

VERSION = '1.0.0'
BUILD_ID = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
RESOURCE_NAMES = ('system', 'services', 'containers', 'sessions', 'repositories', 'preferences')
LIST_LIMIT = 500
PREFERENCE_KEYS = ('panel_order', 'hidden_panels', 'hidden_apps', 'newtab_apps', 'modal_apps',
                   'yolo_apps', 'custom_apps', 't3_tile', 'poll_ms')


def number(value, minimum=None, maximum=None, integer=False):
    if type(value) not in (int, float):
        return None
    try:
        if not math.isfinite(value):
            return None
    except OverflowError:
        return None
    if minimum is not None and value < minimum or maximum is not None and value > maximum:
        return None
    if integer and int(value) != value:
        return None
    return int(value) if integer else value


def text(value, limit=256):
    return value[:limit] if isinstance(value, str) and value else None


def system(raw, identity):
    mac = identity['platform'] == 'macos'
    cpu, memory, temps = (raw.get(key) or {} for key in ('cpu', 'mem', 'temps'))
    kb = lambda value: int(value * 1024) if number(value, 0) is not None else None
    disks = [{
        'id': row['label'], 'mountpoint': row['label'],
        'total_bytes': number(row.get('total'), 0, integer=True),
        'used_bytes': number(row.get('used'), 0, integer=True),
        'usage_percent': number(row.get('pct'), 0, 100),
    } for row in (raw.get('disks') or [])[:32] if isinstance(row, dict) and text(row.get('label'))]
    load = raw.get('load') or []
    return {
        'identity': {'id': 'self', **identity},
        'uptime_seconds': number(raw.get('uptime_s'), 0),
        'cpu': {'usage_percent': number(cpu.get('pct'), 0, 100),
                'logical_count': number(raw.get('ncpu'), 1, integer=True)},
        # macOS's collector measures memory pressure, not occupied RAM.
        'memory': {'total_bytes': kb(memory.get('total_kb')),
                   'used_bytes': None if mac else kb(memory.get('used_kb')),
                   'usage_percent': number(memory.get('pct'), 0, 100),
                   'percent_kind': 'pressure' if mac else 'used'},
        'load_average': [number(load[i], 0) if i < len(load) else None for i in range(3)],
        'disks': disks,
        'temperature': {'cpu_celsius': number(temps.get('cpu')),
                        'gpu_celsius': number(temps.get('gpu'))},
    }


def items(kind, rows, manager='systemd', frozen=False):
    """Project named fields only: additions to legacy collectors cannot leak."""
    result = []
    for row in rows[:LIST_LIMIT]:
        key = row.get('unit' if kind == 'services' else 'name') if isinstance(row, dict) else None
        if not isinstance(key, str) or not key or len(key) > 1024:
            continue
        if kind == 'services':
            result.append({'id': key, 'manager': manager,
                           'state': text(row.get('active')) if row.get('active') != '?' else 'unknown',
                           'detail': text(row.get('sub')) if row.get('sub') != '?' else None})
        elif kind == 'containers':
            result.append({'id': key, 'name': key, 'state': text(row.get('state')),
                           'status_message': text(row.get('status'), 1024), 'project': text(row.get('project'))})
        elif kind == 'sessions':
            windows = row.get('windows')
            if isinstance(windows, str) and re.fullmatch(r'[0-9]{1,9}', windows):
                windows = int(windows)
            result.append({'id': key, 'name': key, 'window_count': number(windows, 0, integer=True),
                           'attached': {'attached': True, 'detached': False}.get(row.get('attached')),
                           'frozen': frozen, 'activity_label': text(row.get('activity'))})
        elif kind == 'repositories':
            result.append({'id': key, 'name': key, 'branch': text(row.get('branch')),
                           'changed_files': number(row.get('dirty'), 0, integer=True),
                           'last_commit_subject': text(row.get('last_subject'), 1024)})
    return result


def tile_url(value):
    if not isinstance(value, str) or not 0 < len(value) <= 200 or re.search(r'[\x00-\x20\\]', value):
        return False
    if value.startswith('/'):
        return not value.startswith('//')
    try:
        parsed = urllib.parse.urlsplit(value)
        return parsed.scheme in ('http', 'https') and bool(parsed.hostname) and not (parsed.username or parsed.password)
    except ValueError:
        return False


def validate_preferences(changes):
    if not isinstance(changes, dict) or changes.keys() - set(PREFERENCE_KEYS):
        raise ValueError('Only documented dashboard preference fields are accepted')
    for key, value in changes.items():
        if key == 'poll_ms':
            valid = type(value) is int and 2000 <= value <= 60000
        elif key == 't3_tile':
            valid = value in ('pwa', 'app')
        elif key == 'custom_apps':
            valid = isinstance(value, list) and len(value) <= 12 and all(
                isinstance(tile, dict) and set(tile) == {'name', 'url'}
                and isinstance(tile['name'], str) and 0 < len(tile['name']) <= 40
                and tile_url(tile['url']) for tile in value)
        else:
            valid = (isinstance(value, list) and len(value) <= 100
                     and all(isinstance(v, str) and 0 < len(v) <= 256 for v in value)
                     and len(set(value)) == len(value))
        if not valid:
            raise ValueError('Invalid preference: ' + key)
    return changes


def preferences(settings, defaults):
    """A typed view of legacy preferences, with invalid legacy entries omitted."""
    values = {}
    for key in PREFERENCE_KEYS:
        value = copy.deepcopy(settings.get(key, defaults[key]))
        if key == 'custom_apps':
            value = [v for v in value if isinstance(v, dict) and set(v) == {'name', 'url'}
                     and isinstance(v['name'], str) and 0 < len(v['name']) <= 40 and tile_url(v['url'])][:12] if isinstance(value, list) else []
        elif isinstance(defaults[key], list):
            value = list(dict.fromkeys(v for v in value if isinstance(v, str) and 0 < len(v) <= 256))[:100] if isinstance(value, list) else []
        try:
            validate_preferences({key: value})
        except ValueError:
            value = copy.deepcopy(defaults[key])
        values[key] = value
    # Include unprojected legacy values in the revision: concurrent legacy edits
    # must still invalidate an earlier read, even when their types were invalid.
    revision = hashlib.sha256(json.dumps(settings, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()
    return {'revision': revision, 'values': values}


def obj(properties, required=None, closed=False):
    return {'type': 'object', 'properties': properties,
            'required': list(properties) if required is None else required,
            'additionalProperties': not closed}


def array(items, maximum=None):
    return {'type': 'array', 'items': items, **({'maxItems': maximum} if maximum is not None else {})}


def nullable(kind='string', **kwargs):
    return {'type': [kind, 'null'], **kwargs}


def ref(name):
    return {'$ref': '#/components/schemas/' + name}


STRING = {'type': 'string'}
BOOL = {'type': 'boolean'}
PERCENT = nullable('number', minimum=0, maximum=100)
COUNT = nullable('integer', minimum=0)
TEXT = nullable()
REVISION = {'type': 'string', 'pattern': '^[0-9a-f]{64}$'}
ID = {'type': 'string', 'pattern': '^[0-9]{10}-[0-9a-f]{32}$'}
PREFS = {key: {'type': 'array', 'items': {'type': 'string', 'minLength': 1, 'maxLength': 256},
              'maxItems': 100, 'uniqueItems': True} for key in PREFERENCE_KEYS}
PREFS.update({
    'poll_ms': {'type': 'integer', 'minimum': 2000, 'maximum': 60000},
    't3_tile': {'type': 'string', 'enum': ['pwa', 'app']},
    'custom_apps': array(obj({'name': {'type': 'string', 'minLength': 1, 'maxLength': 40},
                             'url': {'type': 'string', 'minLength': 1, 'maxLength': 200,
                                     'description': 'Root-relative (not //) or HTTP(S), without credentials, whitespace, controls or backslashes.'}}, closed=True), 12),
})
SCHEMAS = {
    'Error': obj({'ok': {'const': False}, 'error': STRING, 'output': STRING}),
    'ResourceProblem': obj({'code': STRING, 'message': STRING}),
    'System': obj({
        'identity': obj({'id': {'const': 'self'}, 'hostname': STRING,
                         'platform': STRING, 'os_name': STRING, 'os_icon': STRING}),
        'uptime_seconds': nullable('number', minimum=0),
        'cpu': obj({'usage_percent': PERCENT, 'logical_count': nullable('integer', minimum=1)}),
        'memory': obj({'total_bytes': COUNT, 'used_bytes': COUNT, 'usage_percent': PERCENT,
                       'percent_kind': {'enum': ['used', 'pressure']}}),
        'load_average': {**array(nullable('number', minimum=0), 3), 'minItems': 3},
        'disks': array(obj({'id': STRING, 'mountpoint': STRING, 'total_bytes': COUNT,
                            'used_bytes': COUNT, 'usage_percent': PERCENT}), 32),
        'temperature': obj({'cpu_celsius': nullable('number'), 'gpu_celsius': nullable('number')}),
    }),
    'Service': obj({'id': STRING, 'manager': {'enum': ['systemd', 'launchd', 'native']},
                    'state': TEXT, 'detail': TEXT}),
    'Container': obj({'id': STRING, 'name': STRING, 'state': TEXT, 'status_message': TEXT, 'project': TEXT}),
    'Session': obj({'id': STRING, 'name': STRING, 'window_count': COUNT, 'attached': nullable('boolean'),
                    'frozen': BOOL, 'activity_label': TEXT}),
    'Repository': obj({'id': STRING, 'name': STRING, 'branch': TEXT, 'changed_files': COUNT,
                       'last_commit_subject': TEXT}),
    'Preferences': obj({'revision': REVISION, 'values': obj(PREFS)}),
    'PreferencePatch': obj({'revision': REVISION, 'changes': obj(PREFS, required=[], closed=True)}, closed=True),
    'OperationRequest': obj({'id': ID, 'action': {'type': 'string', 'description': 'One of capabilities.operations.actions on this destination.'}}, closed=True),
    'Operation': obj({'id': ID, 'action': STRING,
                      'state': {'enum': ['queued', 'running', 'succeeded', 'failed', 'timed_out', 'interrupted']},
                      'created_at': {'type': 'number'}, 'finished_at': nullable('number'),
                      'exit_code': nullable('integer'), 'cursor': {'type': 'integer', 'minimum': 0},
                      'events': array(obj({'seq': {'type': 'integer', 'minimum': 1}, 'text': STRING}), 512),
                      'truncated': BOOL, 'message': STRING}),
    'OperationHealth': obj({'ok': {'const': True}, 'protocol': {'const': 1},
                            'records': {'type': 'integer', 'minimum': 0}, 'build': REVISION}),
    'KeeperEvent': obj({'seq': {'type': 'integer', 'minimum': 1}, 'type': {'enum': ['text', 'tool_call', 'tool_result', 'done', 'error']},
                        'name': {'type': 'string', 'description': '"owner" for the sent message, the tool name for tool events, else empty.'},
                        'text': STRING, 'at': {'type': 'number'}}),
    'KeeperSummary': obj({'id': ID, 'provider': {'enum': ['claude', 'codex']}, 'session_id': TEXT, 'host': STRING, 'owner': STRING,
                          'created_at': {'type': 'number'}, 'updated_at': {'type': 'number'}, 'summary': STRING,
                          'state': {'enum': ['idle', 'running', 'succeeded', 'failed', 'timed_out', 'cancelled', 'interrupted']}}),
    'KeeperConversation': obj({'id': ID, 'provider': {'enum': ['claude', 'codex']}, 'model': STRING, 'session_id': TEXT, 'host': STRING,
                               'owner': STRING, 'created_at': {'type': 'number'}, 'updated_at': {'type': 'number'}, 'summary': STRING,
                               'state': {'enum': ['idle', 'running', 'succeeded', 'failed', 'timed_out', 'cancelled', 'interrupted']},
                               'turn': {'anyOf': [obj({'id': ID, 'started_at': {'type': 'number'}, 'finished_at': nullable('number'),
                                                       'exit_code': nullable('integer')}), {'type': 'null'}]},
                               'cursor': {'type': 'integer', 'minimum': 0}, 'events': array(ref('KeeperEvent'), 400),
                               'truncated': BOOL, 'message': STRING}),
    'KeeperList': obj({'conversations': array(ref('KeeperSummary'), 64), 'provider': {'enum': ['claude', 'codex']}, 'model': STRING}),
    'KeeperConversationRequest': obj({'id': ID, 'provider': {'enum': ['claude', 'codex']}}, required=['id'], closed=True),
    'KeeperTurnRequest': obj({'conversation': ID, 'turn': ID, 'message': {'type': 'string', 'minLength': 1, 'maxLength': 8000}}, closed=True),
    'KeeperCancelRequest': obj({'conversation': ID}, closed=True),
    'Capabilities': obj({'product': {'const': 'gravedecay'}, 'api_version': {'const': 1},
                         'host': STRING, 'platform': STRING,
                         'routes': obj({'GET': array(STRING), 'POST': array(STRING)}), 'actions': array(STRING),
                         'operations': obj({'protocol': {'const': 1}, 'actions': array(STRING),
                                            'retention_seconds': {'type': 'integer'}, 'new_id_max_age_seconds': {'type': 'integer'}}),
                         'keeper': obj({'provider': {'enum': ['claude', 'codex']}, 'model': STRING,
                                        'tools': array(STRING)}, required=[]),
                         'resource_contract': obj({'version': {'type':'string', 'pattern': r'^1\.[0-9]+\.[0-9]+$'}, 'schema': {'const': 'openapi.json'},
                                                   'sha256': REVISION, 'build': REVISION, 'resources': array(STRING)})}),
    'LegacyObject': {'type': 'object', 'additionalProperties': True,
                     'description': 'Transitional dashboard-shaped payload; see docs/API.md. Not a structured resource contract.'},
}
RESOURCE_DATA = {'system': ref('System'), 'services': array(ref('Service'), LIST_LIMIT),
                 'containers': array(ref('Container'), LIST_LIMIT), 'sessions': array(ref('Session'), LIST_LIMIT),
                 'repositories': array(ref('Repository'), LIST_LIMIT), 'preferences': ref('Preferences')}
for name, data in RESOURCE_DATA.items():
    SCHEMAS[name.title() + 'Resource'] = obj({
        'api_version': {'const': 1}, 'kind': {'const': name},
        'observed_at': {'type': 'string', 'format': 'date-time'},
        'status': {'enum': ['ready', 'partial', 'unavailable', 'paused']},
        'data': {'anyOf': [data, {'type': 'null'}]},
        'error': {'anyOf': [ref('ResourceProblem'), {'type': 'null'}]}, 'truncated': BOOL,
    })


def response(schema, description='Successful response', media='application/json'):
    return {'description': description, 'content': {media: {'schema': schema}}}


def document(routes, base='/grave'):
    """Describe the deployment-independent v1 catalogue, including legacy seams."""
    paths = {}
    header = {'name': 'X-Grave-Client', 'in': 'header', 'required': True,
              'schema': {'type': 'string', 'const': '1'},
              'description': 'Protocol marker, NOT authentication. Owner identity is mandatory via Tailscale Serve or local maintenance.'}
    for method, names in routes.items():
        for name in sorted(names):
            entry = {'operationId': method.lower() + '_' + name.replace('/', '_').replace('-', '_').replace('.', '_'),
                     'summary': method + ' ' + name, 'parameters': [header],
                     'x-grave-contract': 'legacy',
                     'responses': {'200': response(ref('LegacyObject')),
                                   'default': response(ref('LegacyObject'), 'Legacy handler error, or an owner/origin gate error')}}
            if method == 'POST':
                entry['requestBody'] = {'required': True, 'content': {'application/json': {'schema': ref('LegacyObject')}}}
            if name.startswith('resources/'):
                resource = name.split('/')[1]
                entry.update({'x-grave-contract': VERSION, 'responses': {
                    '200': response(ref(resource.title() + 'Resource')),
                    '401': response(obj({'error': STRING}), 'Workspace gateway capability refusal'),
                    **{str(code): response(ref('Error'), description) for code, description in
                       [(400, 'Invalid payload'), (403, 'Identity or origin refused'), (404, 'Unsupported resource'),
                        (409, 'Preference revision conflict'), (501, 'Unsupported deployment'), (503, 'Resource unavailable')]}},
                    'description': 'Owner-private resource. Unknown measurements are null; inspect status and error before using data.'})
                if method == 'POST':
                    entry['requestBody']['content']['application/json']['schema'] = ref('PreferencePatch')
                    entry['requestBody']['description'] = 'Revision-checked patch, at most 65536 encoded bytes; unknown fields are rejected.'
            elif name == 'capabilities':
                entry['x-grave-contract'] = VERSION
                entry['responses']['200'] = response(ref('Capabilities'))
            elif name == 'operations':
                entry['x-grave-contract'] = VERSION
                if method == 'POST':
                    entry['requestBody']['content']['application/json']['schema'] = ref('OperationRequest')
                    entry['responses']['200'] = response(ref('Operation'), 'Existing operation; never replayed')
                    entry['responses']['202'] = response(ref('Operation'), 'New operation accepted')
                else:
                    entry['parameters'] += [
                        {'name': 'id', 'in': 'query', 'schema': ID, 'description': 'Required unless health=1.'},
                        {'name': 'after', 'in': 'query', 'schema': {'type': 'integer', 'minimum': 0, 'maximum': 9999999999}},
                        {'name': 'health', 'in': 'query', 'schema': {'type': 'string', 'const': '1'}, 'description': 'Use alone for the read-only doctor probe.'}]
                    entry['responses']['200'] = response({'oneOf': [ref('Operation'), ref('OperationHealth')]})
            elif name.startswith('keeper'):
                entry['x-grave-contract'] = VERSION
                entry['description'] = 'Gravekeeper conversation on this grave only (single-owner Linux). Conversations bind to their owner and host.'
                if name == 'keeper' and method == 'GET':
                    entry['parameters'] += [
                        {'name': 'id', 'in': 'query', 'schema': ID, 'description': 'Omit to list conversations.'},
                        {'name': 'after', 'in': 'query', 'schema': {'type': 'integer', 'minimum': 0, 'maximum': 9999999999}}]
                    entry['responses']['200'] = response({'oneOf': [ref('KeeperConversation'), ref('KeeperList')]})
                else:
                    body = {'keeper': 'KeeperConversationRequest', 'keeper/turn': 'KeeperTurnRequest', 'keeper/cancel': 'KeeperCancelRequest'}[name]
                    entry['requestBody']['content']['application/json']['schema'] = ref(body)
                    entry['responses']['200'] = response(ref('KeeperConversation'), 'Existing conversation or turn; never re-sent')
                    if name != 'keeper/cancel':
                        entry['responses']['202'] = response(ref('KeeperConversation'), 'New conversation or turn accepted')
            elif name == 'openapi.json':
                entry['x-grave-contract'] = VERSION
                entry['responses']['200'] = response({'type': 'object'}, 'This OpenAPI 3.1 document')
            elif name in ('download',):
                entry['responses']['200'] = response({'type': 'string', 'format': 'binary'}, media='application/octet-stream')
            elif name == 'upload':
                entry['requestBody'] = {'required': True, 'content': {'application/octet-stream': {'schema': {'type': 'string', 'format': 'binary'}}}}
            elif name == 'action-stream':
                entry.pop('requestBody', None)
                entry['parameters'].append({'name': 'action', 'in': 'query', 'required': True, 'schema': STRING})
                entry['responses']['200'] = response(STRING, 'Legacy ephemeral action stream', 'text/event-stream')
            if entry['x-grave-contract'] != 'legacy':
                entry['responses'].setdefault('401', response(obj({'error': STRING}), 'Workspace gateway capability refusal'))
                entry['responses']['default'] = response(ref('Error'), 'Access, validation or availability error')
            paths.setdefault('/' + name, {})[method.lower()] = entry
    return {'openapi': '3.1.1', 'info': {'title': 'Gravedecay owner management API', 'version': VERSION,
             'description': 'Owner authorization is mandatory for EVERY endpoint. Tailscale Serve supplies the authenticated network identity; clients must never manufacture identity headers. Browser origins additionally require explicit trust. OpenAPI has no network-identity scheme; x-grave-authorization describes this out-of-band requirement. No anonymous access is supported. Legacy methods remain transitional; inspect capabilities before calling.'},
            'servers': [{'url': (base or '') + '/api/v1'}],
            'x-grave-authorization': {'required': True, 'network_identity': 'Tailscale Serve owner',
                                      'local_alternative': 'X-Grave-Local-Token (owner-private, loopback maintenance only)',
                                      'browser_origin': 'exact trusted origin or same origin; no cookies'},
            'paths': paths, 'components': {'schemas': copy.deepcopy(SCHEMAS)}}


def digest(document):
    return hashlib.sha256(json.dumps(document, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


if __name__ == '__main__':
    import gravedecay
    rendered = json.dumps(document(gravedecay.CLIENT_ROUTES), indent=2, ensure_ascii=False) + '\n'
    if len(sys.argv) == 3 and sys.argv[1] == '--check':
        if Path(sys.argv[2]).read_text() != rendered:
            sys.exit('OpenAPI snapshot is stale; regenerate with python dashboard/api_contract.py')
    elif len(sys.argv) == 1:
        print(rendered, end='')
    else:
        sys.exit('Usage: api_contract.py [--check docs/openapi.json]')
