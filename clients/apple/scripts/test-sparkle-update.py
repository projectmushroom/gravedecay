#!/usr/bin/env python3
"""Real signed-feed update: reject corrupt bytes, then replace/relaunch an app.

Uses the production driver in a disposable app with an ephemeral signing key.
No production app, Tailscale routes, or user data are touched.
"""
import functools
import http.server
import json
import os
from pathlib import Path
import plistlib
import shutil
import signal
import subprocess
import tempfile
import threading
import time
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[3]
SPARKLE = Path(os.environ['SPARKLE_DIST']).resolve()


def run(*args, **kwargs):
    return subprocess.run([str(arg) for arg in args], check=True, capture_output=True, **kwargs)


def wait_for(description, predicate, timeout=90):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        try:
            if predicate():
                return
        except (OSError, ValueError):
            pass
        time.sleep(.25)
    raise AssertionError(description)


class Handler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


with tempfile.TemporaryDirectory(prefix='grave-update-smoke-') as temporary:
    root = Path(temporary)
    os.chmod(root, 0o700)
    web = root / 'web'
    web.mkdir()
    server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), functools.partial(Handler, directory=str(web)))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    origin = f'http://127.0.0.1:{server.server_port}/'
    binary = root / 'UpdateSmoke'
    run('swiftc', '-parse-as-library', '-swift-version', '5', '-F', SPARKLE,
        '-framework', 'Sparkle', '-Xlinker', '-rpath', '-Xlinker', '@executable_path/../Frameworks',
        ROOT / 'clients/apple/App/Sources/MacAppUpdates.swift',
        ROOT / 'clients/apple/scripts/UpdateSmoke.swift', '-o', binary)
    # The key-generation mode uses the same binary before it has an app bundle.
    keys = json.loads(run(binary, '--generate-key', env=dict(os.environ, DYLD_FRAMEWORK_PATH=str(SPARKLE))).stdout)
    keyfile = root / 'private-key'
    keyfile.write_text(keys['private'])
    keyfile.chmod(0o600)
    del keys['private']
    app = root / 'Update Smoke.app'
    contents = app / 'Contents'
    (contents / 'MacOS').mkdir(parents=True)
    (contents / 'Frameworks').mkdir()
    shutil.copy2(binary, contents / 'MacOS/UpdateSmoke')
    run('ditto', SPARKLE / 'Sparkle.framework', contents / 'Frameworks/Sparkle.framework')
    info = {'CFBundleIdentifier': 'com.projectmushroom.gravedecay.update-smoke',
            'CFBundleName': 'Update Smoke', 'CFBundleExecutable': 'UpdateSmoke',
            'CFBundlePackageType': 'APPL', 'CFBundleVersion': '0.28.0',
            'CFBundleShortVersionString': '0.28.0', 'LSMinimumSystemVersion': '15.0',
            'LSUIElement': True, 'SmokeRoot': str(root), 'SUFeedURL': origin + 'appcast.xml',
            'SUPublicEDKey': keys['public'], 'SUEnableAutomaticChecks': False,
            'SUAllowsAutomaticUpdates': False, 'SUVerifyUpdateBeforeExtraction': True,
            'SURequireSignedFeed': True, 'NSAppTransportSecurity': {'NSAllowsLocalNetworking': True}}
    entitlements = root / 'entitlements.plist'
    entitlements.write_bytes(plistlib.dumps({'com.apple.security.cs.disable-library-validation': True}))

    def sign(bundle, version):
        values = dict(info, CFBundleVersion=version, CFBundleShortVersionString=version)
        (bundle / 'Contents/Info.plist').write_bytes(plistlib.dumps(values))
        run('codesign', '--force', '--sign', '-', '--options', 'runtime', '--entitlements', entitlements, bundle)

    sign(app, '0.28.0')
    old_archive = web / 'UpdateSmoke-0.28.0.zip'
    run('ditto', '-c', '-k', '--sequesterRsrc', '--keepParent', app, old_archive)
    run(SPARKLE / 'bin/generate_appcast', '--ed-key-file', keyfile,
        '--download-url-prefix', origin, '--maximum-deltas', '0', web)
    old_archive.unlink()  # Publication reuses the feed, not every old archive.
    target = root / 'target/Update Smoke.app'
    run('ditto', app, target)
    sign(target, '0.28.1')
    archive = web / 'UpdateSmoke-0.28.1.zip'
    run('ditto', '-c', '-k', '--sequesterRsrc', '--keepParent', target, archive)
    run(SPARKLE / 'bin/generate_appcast', '--ed-key-file', keyfile,
        '--download-url-prefix', origin, '--maximum-deltas', '0', web)
    run(SPARKLE / 'bin/sign_update', '--verify', '--ed-key-file', keyfile, web / 'appcast.xml')
    versions = [item.text for item in ET.parse(web / 'appcast.xml').findall('.//{http://www.andymatuschak.org/xml-namespaces/sparkle}version')]
    assert versions == ['0.28.1', '0.28.0'], versions
    signed_feed = (web / 'appcast.xml').read_bytes()
    (web / 'appcast.xml').write_bytes(signed_feed.replace(b'0.28.1', b'0.28.2'))
    original = archive.read_bytes()
    archive.write_bytes(original + b'corrupt signed archive')
    log = (root / 'app.log').open('wb')
    process = subprocess.Popen([str(app / 'Contents/MacOS/UpdateSmoke')], stdout=log, stderr=log)
    def state():
        return json.loads((root / 'updates/status.json').read_text())
    def request(attempt):
        path = root / 'updates/request.json'
        path.write_text(json.dumps({'action': 'install', 'current': 'v0.28.0', 'tag': 'v0.28.1',
                                    'requested_at': time.time(), 'attempt': attempt}))
    try:
        wait_for('Tampered feed check did not finish', lambda: not state()['checking'] and state()['message'] != 'Checking for app updates…')
        assert not state()['available'] and not state()['releases'], 'Unverified feed offered an update'
        (web / 'appcast.xml').write_bytes(signed_feed)
        (root / 'updates/request.json').write_text(json.dumps({'action': 'check'}))
        wait_for('Signed feed was not discovered', lambda: state()['available'])
        request('corrupt-archive')
        wait_for('Corrupt archive was not rejected', lambda: state()['state'] == 'failed')
        assert not (root / 'before-relaunch').exists(), 'Invalid update tried to restart the app'
        assert plistlib.loads((contents / 'Info.plist').read_bytes())['CFBundleVersion'] == '0.28.0'
        archive.write_bytes(original)
        request('valid-archive')
        wait_for('Valid update did not relaunch and confirm its version',
                 lambda: state()['state'] == 'ok' and state()['current'] == 'v0.28.1', timeout=120)
        assert (root / 'before-relaunch').exists()
        assert plistlib.loads((contents / 'Info.plist').read_bytes())['CFBundleVersion'] == '0.28.1'
        print('Sparkle: feed history, tampered feed/archive rejection, exact release, app replacement and relaunch passed.')
    except Exception:
        print('Updater status:', state() if (root / 'updates/status.json').exists() else 'missing')
        log.flush()
        print((root / 'app.log').read_text(errors='replace')[-4000:])
        raise
    finally:
        if process.poll() is None:
            process.terminate()
        process.wait(timeout=10)
        try:
            os.kill(int((root / 'pid').read_text()), signal.SIGTERM)
        except (OSError, ValueError):
            pass
        server.shutdown()
        log.close()
