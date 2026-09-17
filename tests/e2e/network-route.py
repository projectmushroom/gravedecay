#!/usr/bin/env python3
"""Check real gateway, SSE, Unix boundary and stateful migration Serve config."""
import http.client
import json
import os
from pathlib import Path
import subprocess
import sys

assert os.geteuid()==0, 'run only in the disposable appliance'

def run(*args,ok=True):
    result=subprocess.run(args,capture_output=True,text=True,timeout=90)
    assert (result.returncode==0)==ok, f'{args[:3]}: {result.stdout} {result.stderr}'
    return result

def config(): return json.loads(run('tailscale','serve','status','--json').stdout)
origin='e2ebox.tail-e2e.ts.net:443'
if sys.argv[1]=='prepare':
    assert config()['Web'][origin]['Handlers']['/net']['Proxy']=='http://127.0.0.1:4714'
    run('tailscale','serve','--bg','--https=3000','http://127.0.0.1:3000')
    print('Prepared migration with real legacy /net mount and independent preview')
else:
    token=Path('/srv/dev/config/secrets/gateway-token').read_text().strip()
    def request(path,login=None,prefixed=True,stream=False):
        connection=http.client.HTTPConnection('127.0.0.1',4710,timeout=5)
        headers={'Host':'e2ebox.tail-e2e.ts.net','X-Grave-Role':'admin'}
        if login: headers['Tailscale-User-Login']=login
        target=('/_grave_proxy/'+token if prefixed else '')+path
        connection.request('GET',target,headers=headers); response=connection.getresponse()
        body=response.readline() if stream else response.read()
        status=response.status; content_type=response.getheader('Content-Type','')
        connection.close(); return status,content_type,body
    assert request('/net/','mole@example.com')[0]==200
    status,kind,body=request('/net/events','mole@example.com',stream=True)
    assert status==200 and kind=='text/event-stream' and body.startswith(b'event: history')
    for path in ('/net','/net/','/net/events','/net/healthz'):
        assert request(path,'bob@example.com')[0]==403
        assert request(path,'nobody@example.com')[0]==401
        assert request(path)[0]==401
        assert request(path,'mole@example.com',prefixed=False)[0]==401
    run('runuser','-u','grave-bob','--','curl','--max-time','3','-sf','http://127.0.0.1:4714/healthz',ok=False)
    run('grave','__multiuser-serve-check')
    before=config()['Web']['e2ebox.tail-e2e.ts.net:3000']
    run('tailscale','serve','--bg','--https=443','--set-path=/net','http://127.0.0.1:4714')
    failure=run('grave','__multiuser-serve-check',ok=False)
    assert 'Serve configuration differs' in failure.stderr
    doctor=run('runuser','-u','mole','--','grave','doctor','--no-page',ok=False)
    assert 'gateway is the sole Serve origin' in doctor.stdout
    assert '1 check(s) failed' in doctor.stdout
    for _ in range(2): run('grave','__multiuser-serve')
    run('grave','__multiuser-serve-check')
    assert list(config()['Web'][origin]['Handlers'])==['/']
    assert config()['Web']['e2ebox.tail-e2e.ts.net:3000']==before
    assert token not in doctor.stdout+doctor.stderr
    print('Admin network route, SSE, stale Serve drift and preview preservation: PASS')
