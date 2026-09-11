"""Owner-gated HTTP handlers use this private mailbox to reach the Mac updater.

Only the app installs updates. No URL, executable, or shell command crosses
this boundary; requests name a release already offered by the signed appcast.
"""
import json
import os
from pathlib import Path
import re
import time
import uuid


def directory():
    return Path(os.environ['GRAVE_ROOT']) / 'updates'


def status():
    try:
        path = directory() / 'status.json'
        if path.stat().st_size > 65536 or time.time() - path.stat().st_mtime > 30:
            raise ValueError('App updater is not responding')
        result = json.loads(path.read_text())
        if not isinstance(result, dict) or result.get('state') not in ('idle', 'queued', 'running', 'ok', 'failed'):
            raise ValueError('Invalid app updater status')
        request = directory() / 'request.json'
        if request.exists():
            pending = json.loads(request.read_text())
            result.update(state='queued', attempt=pending['attempt'], target=pending['tag'],
                          message='Waiting for the Mac app updater')
        result['dashboard_current'] = result.get('target') == result.get('current') if result['state'] == 'ok' else True
        return result
    except (OSError, ValueError, KeyError) as error:
        return {'state': 'failed', 'message': str(error), 'available': False,
                'releases': [], 'current': '', 'channel': 'release'}


def request(data, check=False):
    current = status()
    if not current.get('current'):
        return 503, {'output': 'Mac app updater is unavailable'}
    if current['state'] in ('queued', 'running'):
        return 409, {'output': 'An update is already queued or running'}
    if check and data:
        return 400, {'output': 'Update check takes no arguments'}
    if check and not data:
        tag = ''
    elif data == {'channel': 'release'}:
        tag = current.get('latest', '')
    elif set(data) == {'tag'}:
        tag = data['tag']
    else:
        return 400, {'output': 'Choose a signed Mac app release'}
    if not check and (not isinstance(tag, str) or not re.fullmatch(r'v[0-9]+\.[0-9]+\.[0-9]+', tag)):
        return 400, {'output': 'Invalid release tag'}
    if not check and tag not in current.get('releases', []):
        return 409, {'output': 'This release is not offered by the Mac app updater; check again'}
    payload = {'tag': tag, 'current': current['current'], 'attempt': uuid.uuid4().hex,
               'requested_at': time.time(), 'action': 'check' if check else 'install'}
    path = directory() / 'request.json'
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w') as stream:
            json.dump(payload, stream)
    except FileExistsError:
        return 409, {'output': 'An update is already queued'}
    except OSError:
        return 503, {'output': 'Mac app updater is unavailable'}
    return 202, {'ok': True, 'output': 'App update queued; the dashboard will reconnect'}
