#!/usr/bin/env python3
"""Identity-aware gateway for Tailscale Serve."""
import json, os, selectors, socket, socketserver, subprocess, threading, time
from pathlib import Path

ROOT=Path(os.environ.get("GRAVE_ROOT","/srv/dev"))
REGISTRY=Path(os.environ.get("GRAVE_WORKSPACE_REGISTRY",ROOT/"config/workspaces.json"))
HOST="127.0.0.1"; PORT=int(os.environ.get("GRAVE_GATEWAY_PORT","4710"))
ADMIN_DASH_PORT=int(os.environ.get("GRAVE_ADMIN_DASH_PORT","4712"))
TOKEN_FILE=Path(os.environ.get("GRAVE_GATEWAY_TOKEN_FILE",ROOT/"config/secrets/gateway-token"))
AUDIT=Path(os.environ.get("GRAVE_AUDIT_LOG",ROOT/"logs/audit.jsonl"))
MAX_HEADER=65536
ADMIN_PREFIXES=("/grave/api/admin/",)
ADMIN_ACTIONS={"gaming","gaming-kill","developer","restart-t3","update-t3","update-grave","reboot","bootmode-developer","bootmode-gaming","gamewatch-on","gamewatch-off","doctor"}

def audit(event, actor=None, target=None, result="ok"):
    AUDIT.parent.mkdir(parents=True,exist_ok=True)
    record={"ts":time.strftime("%Y-%m-%dT%H:%M:%SZ",time.gmtime()),"event":event,"actor":actor,"target":target,"result":result}
    fd=os.open(AUDIT,os.O_WRONLY|os.O_CREAT|os.O_APPEND,0o600)
    try: os.write(fd,(json.dumps(record,separators=(",",":"))+"\n").encode())
    finally: os.close(fd)

def identities():
    override=os.environ.get("GRAVE_TAILSCALE_STATUS")
    try:
        raw=Path(override).read_text() if override else subprocess.check_output(["tailscale","status","--json"],text=True,timeout=3)
        status=json.loads(raw)
    except Exception: return {}
    return {str(uid):info.get("LoginName","") for uid,info in status.get("User",{}).items()}

def registry():
    data=json.loads(REGISTRY.read_text())
    if data.get("version") != 1: raise ValueError("unsupported registry")
    return data["workspaces"]

def resolve(headers):
    login=headers.get("tailscale-user-login","")
    stable=headers.get("tailscale-user-id","").removeprefix("ts:")
    known=identities()
    if stable and known.get(stable) != login: return None,"malformed"
    if not stable:
        matches=[uid for uid,value in known.items() if value == login]
        if len(matches) != 1: return None,"unknown"
        stable=matches[0]
    matches=[w for w in registry() if w.get("id") == f"ts:{stable}"]
    if len(matches) != 1: return None,"unknown"
    w=matches[0]
    if not w.get("enabled"): return w,"disabled"
    # Login is display metadata. A confirmed rename is safe and does not alter routing.
    return w,"ok"

def response(code,message):
    body=(message+"\n").encode(); reason={400:"Bad Request",401:"Unauthorized",403:"Forbidden",502:"Bad Gateway"}.get(code,"Error")
    return f"HTTP/1.1 {code} {reason}\r\nContent-Type: text/plain\r\nContent-Length: {len(body)}\r\nCache-Control: no-store\r\nConnection: close\r\n\r\n".encode()+body

def read_head(sock):
    data=b""
    while b"\r\n\r\n" not in data and len(data)<MAX_HEADER:
        chunk=sock.recv(8192)
        if not chunk: break
        data+=chunk
    head,sep,rest=data.partition(b"\r\n\r\n")
    if not sep: raise ValueError("incomplete headers")
    lines=head.decode("iso-8859-1").split("\r\n"); method,path,version=lines[0].split(" ",2)
    headers={}
    for line in lines[1:]:
        name,colon,value=line.partition(":")
        if not colon: raise ValueError("malformed header")
        headers[name.strip().lower()]=value.strip()
    return method,path,version,headers,lines[1:],rest

def admin_request(method,path,rest):
    clean=path.split("?",1)[0]
    if any(clean.startswith(p) for p in ADMIN_PREFIXES): return True
    if clean in ("/grave/api/action","/grave/api/action-stream"):
        query=path.partition("?")[2]
        action=next((v for kv in query.split("&") if kv.startswith("action=") for v in [kv.partition("=")[2]]),"")
        if rest and b'"action"' in rest:
            try: action=json.loads(rest)["action"]
            except Exception: pass
        return action in ADMIN_ACTIONS
    return False

def relay(a,b):
    sel=selectors.DefaultSelector(); sel.register(a,selectors.EVENT_READ,b); sel.register(b,selectors.EVENT_READ,a)
    while sel.get_map():
        for key,_ in sel.select(timeout=120):
            src=key.fileobj; dst=key.data
            try: data=src.recv(65536)
            except OSError: data=b""
            if not data:
                try: dst.shutdown(socket.SHUT_WR)
                except OSError: pass
                sel.unregister(src); continue
            dst.sendall(data)

class Handler(socketserver.BaseRequestHandler):
    def handle(self):
        try: method,path,version,headers,raw_headers,rest=read_head(self.request)
        except Exception: self.request.sendall(response(400,"bad request")); return
        if path.split("?",1)[0] == "/healthz": self.request.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok"); return
        try: token=TOKEN_FILE.read_text().strip()
        except OSError: token=""
        prefix=f"/_grave_proxy/{token}"
        if len(token)<32 or not path.startswith(prefix+"/"):
            audit("untrusted_proxy",None,None,"denied"); self.request.sendall(response(401,"untrusted proxy")); return
        path=path[len(prefix):]
        try: length=int(headers.get("content-length","0"))
        except ValueError: self.request.sendall(response(400,"bad content length")); return
        if length > 1024*1024*1024: self.request.sendall(response(400,"request too large")); return
        while len(rest)<length:
            chunk=self.request.recv(min(65536,length-len(rest)))
            if not chunk: break
            rest+=chunk
        if len(rest)<length: self.request.sendall(response(400,"incomplete request body")); return
        w,state=resolve(headers)
        if state != "ok":
            code=401 if state in ("unknown","malformed") else 403
            audit("access_denied",headers.get("tailscale-user-login"),None,state); self.request.sendall(response(code,state)); return
        if admin_request(method,path,rest) and w["role"] != "admin":
            audit("admin_denied",w["id"],w["slug"],"forbidden"); self.request.sendall(response(403,"administrator access required")); return
        if admin_request(method,path,rest): audit("administrative_action",w["id"],w["slug"])
        if path.split("?",1)[0]=="/grave/api/action" and b't3-pair' in rest: audit("pairing_created",w["id"],w["slug"])
        clean=path
        if path == "/grave": clean="/grave/"
        if clean.startswith("/grave/"): kind="dash"; upstream_path=clean[len("/grave"):]
        elif clean.startswith("/term/") or clean == "/term": kind="term"; upstream_path=clean[len("/term"):] or "/"
        else: kind="t3"; upstream_path=clean
        if kind=="term" or headers.get("upgrade","").lower()=="websocket": audit("session_created",w["id"],w["slug"])
        # Appliance mutations execute in the legacy owner dashboard service,
        # whose Unix account alone has the scoped sudoers grant. Developers
        # are denied above; the admin workspace otherwise keeps private state.
        port=ADMIN_DASH_PORT if kind=="dash" and admin_request(method,path,rest) and w["role"]=="admin" else w["ports"][kind]
        stripped={"tailscale-user-login","tailscale-user-id","x-grave-workspace","x-grave-role","x-forwarded-host","x-forwarded-user"}
        forwarded=[line for line in raw_headers if line.partition(":")[0].strip().lower() not in stripped]
        forwarded += [f"X-Grave-Workspace: {w['slug']}",f"X-Grave-Role: {w['role']}",f"X-Forwarded-User: {w['id']}"]
        request=(f"{method} {upstream_path} {version}\r\n"+"\r\n".join(forwarded)+"\r\n\r\n").encode("iso-8859-1")+rest
        try:
            with socket.create_connection(("127.0.0.1",port),timeout=5) as backend:
                backend.sendall(request); relay(self.request,backend)
        except OSError:
            self.request.sendall(response(502,"workspace service unavailable"))

class Server(socketserver.ThreadingTCPServer):
    daemon_threads=True; allow_reuse_address=True

def main():
    with Server((HOST,PORT),Handler) as server: server.serve_forever()
if __name__ == "__main__": main()
