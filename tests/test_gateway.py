import json, os, socket, socketserver, subprocess, tempfile, threading, time, unittest
from pathlib import Path

GATEWAY=Path(__file__).parents[1]/"dashboard/gateway.py"

class Echo(socketserver.BaseRequestHandler):
    def handle(self):
        data=self.request.recv(65536)
        if b"Upgrade: websocket" in data:
            self.request.sendall(b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n\r\n")
            payload=self.request.recv(1024); self.request.sendall(payload); return
        body=(self.server.label+"\n").encode()
        self.request.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: "+str(len(body)).encode()+b"\r\nConnection: close\r\n\r\n"+body)

class GatewayTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.root=Path(self.temp.name); self.servers=[]
        ports=[]
        for label in ("alice-t3","alice-term","alice-dash","bob-t3","bob-term","bob-dash"):
            server=socketserver.ThreadingTCPServer(("127.0.0.1",0),Echo); server.label=label
            threading.Thread(target=server.serve_forever,daemon=True).start(); self.servers.append(server); ports.append(server.server_address[1])
        workspaces=[]
        for i,(uid,login,slug,role) in enumerate((("123","a@example.com","alice","admin"),("456","b@example.com","bob","developer"))):
            p=ports[i*3:i*3+3]
            workspaces.append({"id":"ts:"+uid,"login":login,"slug":slug,"role":role,"enabled":True,"projects":[],"provider":{"llm":True},"ports":{"t3":p[0],"term":p[1],"dash":p[2]}})
        (self.root/"config").mkdir(); (self.root/"logs").mkdir()
        (self.root/"config/workspaces.json").write_text(json.dumps({"version":1,"workspaces":workspaces}))
        status=self.root/"status.json"; status.write_text(json.dumps({"User":{"123":{"LoginName":"a@example.com"},"456":{"LoginName":"b@example.com"}}}))
        token="a"*64; (self.root/"config/secrets").mkdir(); (self.root/"config/secrets/gateway-token").write_text(token)
        probe=socket.socket(); probe.bind(("127.0.0.1",0)); self.port=probe.getsockname()[1]; probe.close(); self.prefix=f"/_grave_proxy/{token}"
        env={**os.environ,"GRAVE_ROOT":str(self.root),"GRAVE_GATEWAY_PORT":str(self.port),"GRAVE_TAILSCALE_STATUS":str(status)}
        self.proc=subprocess.Popen([GATEWAY],env=env)
        for _ in range(100):
            try:
                with socket.create_connection(("127.0.0.1",self.port),timeout=.05): break
            except OSError: pass
            time.sleep(.02)
    def tearDown(self):
        self.proc.terminate(); self.proc.wait(timeout=3)
        for server in self.servers: server.shutdown(); server.server_close()
        self.temp.cleanup()
    def request(self,path,login=None,uid=None,extra=""):
        s=socket.create_connection(("127.0.0.1",self.port))
        headers=""
        if login: headers+=f"Tailscale-User-Login: {login}\r\n"
        if uid: headers+=f"Tailscale-User-Id: {uid}\r\n"
        target=path if path=="/healthz" else self.prefix+path
        s.sendall(f"GET {target} HTTP/1.1\r\nHost: box\r\n{headers}{extra}Connection: close\r\n\r\n".encode())
        out=b""
        while True:
            chunk=s.recv(65536)
            if not chunk: break
            out+=chunk
        s.close()
        return out
    def test_principals_route_to_fixed_distinct_backends(self):
        self.assertIn(b"alice-t3",self.request("/", "a@example.com"))
        self.assertIn(b"alice-term",self.request("/term/", "a@example.com"))
        self.assertIn(b"bob-dash",self.request("/grave/api/state", "b@example.com"))
    def test_spoofed_workspace_headers_cannot_select_upstream(self):
        out=self.request("/", "b@example.com",extra="X-Grave-Workspace: alice\r\nX-Forwarded-User: ts:123\r\n")
        self.assertIn(b"bob-t3",out); self.assertNotIn(b"alice-t3",out)
    def test_missing_unknown_and_mismatched_identity_are_denied(self):
        self.assertIn(b"401",self.request("/"))
        self.assertIn(b"401",self.request("/","nobody@example.com"))
        self.assertIn(b"401",self.request("/","a@example.com","456"))
        s=socket.create_connection(("127.0.0.1",self.port)); s.sendall(b"GET / HTTP/1.1\r\nHost: local\r\n\r\n")
        self.assertIn(b"untrusted proxy",s.recv(4096)); s.close()
    def test_health_is_identity_free_and_developer_admin_action_is_forbidden(self):
        self.assertIn(b"200 OK",self.request("/healthz"))
        out=self.request("/grave/api/action-stream?action=reboot","b@example.com")
        self.assertIn(b"403",out)
        audit=(self.root/"logs/audit.jsonl").read_text(); self.assertIn("admin_denied",audit)
    def test_developer_posted_admin_action_is_forbidden(self):
        body=b'{"action":"gaming"}'
        s=socket.create_connection(("127.0.0.1",self.port))
        req=(f"POST {self.prefix}/grave/api/action HTTP/1.1\r\nHost: box\r\nTailscale-User-Login: b@example.com\r\nContent-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode()+body)
        s.sendall(req); out=s.recv(4096); s.close(); self.assertIn(b"403",out)
    def test_websocket_upgrade_is_bidirectional(self):
        s=socket.create_connection(("127.0.0.1",self.port))
        s.sendall(f"GET {self.prefix}/socket HTTP/1.1\r\nHost: box\r\nTailscale-User-Login: a@example.com\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n\r\n".encode())
        self.assertIn(b"101 Switching",s.recv(4096)); s.sendall(b"workspace-frame")
        self.assertEqual(s.recv(1024),b"workspace-frame"); s.close()

if __name__ == "__main__": unittest.main()
