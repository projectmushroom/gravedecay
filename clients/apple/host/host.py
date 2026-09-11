#!/usr/bin/env python3
"""The Mac app owns this process through stdin; EOF also stops it after a crash.

All dashboard data, web routes and access checks come from the shared backend.
The application bundle is immutable; only the dedicated root holds user state.
"""
import argparse
import json
import os
from pathlib import Path
import sys
import threading
import traceback
import urllib.error
import urllib.request


def configure(root, port, network_port):
    os.umask(0o077)
    root = Path(root).resolve()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    bundle = Path(__file__).resolve().parent
    os.environ.update(PYTHONDONTWRITEBYTECODE='1', PYTHONNOUSERSITE='1', GRAVE_ROOT=str(root), GRAVEDECAY_PLATFORM='macos',
                      GRAVEDECAY_MACOS_NATIVE='1', GRAVEDECAY_MACOS_AGENTS='0',
                      GRAVEDECAY_PORT=str(port), GRAVEDECAY_BASE='/grave',
                      GRAVEDECAY_APPS='📡 Network=/net/',
                      GRAVENET_PLATFORM='macos', GRAVENET_BIND='127.0.0.1',
                      GRAVENET_PORT=str(network_port), GRAVENET_WEB=str(bundle / 'net'))
    sys.path.insert(0, str(bundle))  # -I excludes even the script directory.


def check(port, network_port):
    """Doctor contract: the web app must work, not just its health endpoint."""
    import gravedecay as dashboard
    for path, content in (('/', b'<!DOCTYPE html>'), ('/manifest.webmanifest', b'"start_url"'),
                          ('/sw.js', b'fetch'), ('/icon-192.png', b'\x89PNG')):
        with urllib.request.urlopen(f'http://127.0.0.1:{port}{path}', timeout=15) as response:
            assert response.status == 200 and content.lower() in response.read().lower(), path
    with urllib.request.urlopen(f'http://127.0.0.1:{port}/icon-192.png', timeout=5) as response:
        png = response.read(24)
        assert png[16:24] == (192).to_bytes(4, 'big') * 2
    with urllib.request.urlopen(f'http://127.0.0.1:{port}/api/v1/summary', timeout=15) as response:
        assert json.load(response)['links']['dashboard'] == '/grave/'
    try:
        urllib.request.urlopen(f'http://127.0.0.1:{port}/api/auth-check', timeout=5)
    except urllib.error.HTTPError as error:
        assert error.code == 403
    else:
        raise AssertionError('Headerless request was authorized')
    request = urllib.request.Request(f'http://127.0.0.1:{port}/api/auth-check',
                                     headers={'X-Grave-Local-Token': dashboard.local_token()})
    with urllib.request.urlopen(request, timeout=5) as response:
        assert response.status == 200
    with urllib.request.urlopen(f'http://127.0.0.1:{network_port}/healthz', timeout=5) as response:
        assert response.status == 200


def serve():
    import gravedecay as dashboard
    import gravenet as network
    dashboard.local_token(create=True)
    # Bind both before starting either: port collisions cannot leave half a host.
    with dashboard.ThreadingHTTPServer(('127.0.0.1', dashboard.PORT), dashboard.Handler) as web:
        with network.ThreadingHTTPServer(('127.0.0.1', network.PORT), network.Handler) as net:
            network.SLOW.start()
            network.SAMPLER.start()
            threading.Thread(target=web.serve_forever, daemon=True).start()
            threading.Thread(target=net.serve_forever, daemon=True).start()
            port, network_port = web.server_port, net.server_port
            check(port, network_port)
            print(json.dumps({'ready': True, 'port': port, 'network_port': network_port}), flush=True)
            sys.stdin.buffer.read()  # Parent death/quit closes the pipe; no orphan server.


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', required=True)
    parser.add_argument('--port', type=int, default=4712)
    parser.add_argument('--network-port', type=int, default=4714)
    parser.add_argument('--check', action='store_true')
    args = parser.parse_args()
    configure(args.root, args.port, args.network_port)
    try:
        if args.check:
            check(args.port, args.network_port)
            print('Native host doctor: dashboard, PWA, summary, identity boundary and network pass')
        else:
            serve()
    except Exception:
        traceback.print_exc()
        raise
    finally:
        # HTTP keepalive/SSE clients and in-flight sampling must not keep the child
        # alive after its app has quit. No state writes occur in these threads.
        if not args.check:
            sys.stdout.flush()
            sys.stderr.flush()
            os._exit(0 if sys.exc_info()[0] is None else 1)
