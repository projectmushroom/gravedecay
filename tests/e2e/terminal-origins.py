#!/usr/bin/env python3
"""Probe ttyd handshakes without sending the hello that creates a PTY."""
import argparse
import socket


def handshake(port, origin, path='/ws', host='box.example', headers=()):
    fields = [f'GET {path} HTTP/1.1', f'Host: {host}', 'Upgrade: websocket',
              'Connection: Upgrade', 'Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==',
              'Sec-WebSocket-Version: 13', 'Sec-WebSocket-Protocol: tty', *headers]
    if origin is not None:
        fields.append(f'Origin: {origin}')
    with socket.create_connection(('127.0.0.1', port), timeout=5) as sock:
        sock.sendall(('\r\n'.join(fields) + '\r\n\r\n').encode())
        response = b''
        while b'\r\n' not in response:
            chunk = sock.recv(4096)
            if not chunk:
                break
            response += chunk
        return response.split(b'\r\n', 1)[0]


def verify(port, path='/ws', headers=()):
    for origin, accepted in [('https://box.example', True),
                             ('https://hostile.example', False),
                             ('https://box.example.hostile.example', False),
                             ('https://box.example:8443', False),
                             ('null', False), (None, False)]:
        status = handshake(port, origin, path, headers=headers)
        assert (b' 101 ' in status) == accepted, (origin, status, accepted)
    print('terminal origins: matching accepted; hostile, wrong-port, null and missing rejected')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('port', type=int)
    parser.add_argument('--path', default='/ws')
    parser.add_argument('--gateway-token-file')
    parser.add_argument('--user')
    args = parser.parse_args()
    headers = []
    path = args.path
    if args.gateway_token_file:
        from pathlib import Path
        token = Path(args.gateway_token_file).read_text().strip()
        path = f'/_grave_proxy/{token}/term/ws'
        headers = [f'Tailscale-User-Login: {args.user}']
    verify(args.port, path, headers)
