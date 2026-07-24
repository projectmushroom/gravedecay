#!/usr/bin/env python3
"""gravenet — realtime network flow monitor for the gravedecay appliance.

Samples /proc/net/dev once a second, merges DHCP leases (NetworkManager
shared-mode dnsmasq and plain dnsmasq), neighbours, thunderbolt topology,
tailnet peers and upstream health, and streams JSON snapshots over SSE to
a single self-contained dashboard page (web/net/index.html).

Interface roles are auto-detected — the default-route device is "upstream",
anything with a dnsmasq lease file is "share", wireless is "uplink",
tailscale*/wg*/zt* are "overlay" — and can be overridden per box:

    GRAVENET_ROLES="thunderbolt0=share:tb-share → Mac;enp69s0=upstream:10GbE"

(semicolon-separated iface=kind:label; kind ∈ upstream|share|uplink|overlay).

Runs as root: NetworkManager lease files are 0600 root, and conntrack
counters live under /proc/sys/net/netfilter. Read-only throughout — the
daemon never writes anything and executes no request-derived input.

Env: GRAVENET_PORT (default 4714), GRAVENET_BIND (default 127.0.0.1 — the
page is exposed via `tailscale serve /net`; widen to 0.0.0.0 only if the
LAN clients themselves should load it), GRAVENET_WEB (page directory,
default $GRAVE_ROOT/web/net), GRAVENET_ROLES (see above).
"""

import ipaddress
import json
import glob
import os
import re
import socket
import subprocess
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

GRAVE_ROOT = os.environ.get("GRAVE_ROOT", "/srv/dev")
PORT = int(os.environ.get("GRAVENET_PORT", "4714"))
BIND = os.environ.get("GRAVENET_BIND", "127.0.0.1")
WEB_ROOT = os.environ.get("GRAVENET_WEB", os.path.join(GRAVE_ROOT, "web/net"))
HISTORY = 180          # seconds of per-second samples kept and replayed on connect
SLOW_EVERY = 10        # seconds between lease/neighbour/upstream/tailscale scans

EXCLUDE = re.compile(r"^(lo|veth|br-|docker|ifb|dummy|virbr|vnet)")
OVERLAY = re.compile(r"^(tailscale|wg|zt|tun|utun)")

LEASE_GLOBS = [
    "/var/lib/NetworkManager/dnsmasq-*.leases",   # NM shared-mode, iface in name
    "/var/lib/misc/dnsmasq.leases",               # plain dnsmasq
    "/var/lib/dnsmasq/dnsmasq.leases",
]

KIND_LABEL = {"upstream": "upstream", "share": "shared subnet",
              "uplink": "wifi", "overlay": "overlay", "other": ""}


def parse_role_overrides():
    out = {}
    for part in os.environ.get("GRAVENET_ROLES", "").split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        iface, spec = part.split("=", 1)
        kind, _, label = spec.partition(":")
        out[iface.strip()] = (kind.strip() or "other", label.strip())
    return out


ROLE_OVERRIDES = parse_role_overrides()


def list_ifaces():
    return [n for n in sorted(os.listdir("/sys/class/net")) if not EXCLUDE.match(n)]


def read_counters():
    out = {}
    with open("/proc/net/dev") as f:
        for line in f.readlines()[2:]:
            name, rest = line.split(":", 1)
            v = rest.split()
            out[name.strip()] = (int(v[0]), int(v[8]))  # rx_bytes, tx_bytes
    return out


def iface_static(name):
    base = f"/sys/class/net/{name}"
    def slurp(p):
        try:
            with open(f"{base}/{p}") as f:
                return f.read().strip()
        except OSError:
            return ""
    speed = slurp("speed")
    state = slurp("operstate")
    if state == "unknown" and slurp("carrier") == "1":
        state = "up"          # tun/tap devices (tailscale, wireguard) never report operstate
    return {
        "state": state,
        "speed": int(speed) if speed.lstrip("-").isdigit() and int(speed) > 0 else None,
        "wireless": os.path.isdir(f"{base}/wireless"),
    }


def ip_json(*args):
    try:
        return json.loads(subprocess.run(
            ["ip", "-j", *args], capture_output=True, text=True, timeout=3).stdout or "[]")
    except Exception:
        return []


def addrs_by_iface():
    out = {}
    for e in ip_json("-4", "addr", "show"):
        for a in e.get("addr_info", []):
            out.setdefault(e["ifname"], []).append(f"{a['local']}/{a['prefixlen']}")
    return out


def neighbours():
    out = {}
    for n in ip_json("neigh", "show"):
        if "dst" in n and ":" not in n["dst"]:
            out[n["dst"]] = {"state": (n.get("state") or ["?"])[0],
                             "mac": n.get("lladdr", ""), "dev": n.get("dev", "")}
    return out


def iface_for_ip(ip, addrs):
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return ""
    for iface, cidrs in addrs.items():
        for c in cidrs:
            if addr in ipaddress.ip_network(c, strict=False):
                return iface
    return ""


def leases(addrs):
    rows, seen = [], set()
    for pattern in LEASE_GLOBS:
        for path in glob.glob(pattern):
            m = re.search(r"dnsmasq-(.+)\.leases$", path)
            iface_hint = m.group(1) if m else ""
            try:
                with open(path) as f:
                    lines = f.readlines()
            except OSError:
                continue
            for line in lines:
                p = line.split()
                if len(p) < 4 or p[2] in seen:
                    continue
                seen.add(p[2])
                rows.append({
                    "iface": iface_hint or iface_for_ip(p[2], addrs),
                    "expiry": int(p[0]) if p[0].isdigit() else 0,
                    "mac": p[1], "ip": p[2],
                    "host": p[3] if p[3] != "*" else "",
                })
    return rows


def lease_ifaces():
    out = set()
    for path in glob.glob(LEASE_GLOBS[0]):
        m = re.search(r"dnsmasq-(.+)\.leases$", path)
        if m:
            out.add(m.group(1))
    return out


def thunderbolt():
    devs = []
    for d in glob.glob("/sys/bus/thunderbolt/devices/*/device_name"):
        base = os.path.dirname(d)
        try:
            with open(d) as f:
                name = f.read().strip()
            with open(f"{base}/vendor_name") as f:
                vendor = f.read().strip()
            devs.append({"vendor": vendor, "device": name})
        except OSError:
            pass
    return devs


def default_gateway():
    for r in ip_json("route", "show", "default"):
        return r.get("gateway"), r.get("dev")
    return None, None


def ping(host):
    try:
        r = subprocess.run(["ping", "-c1", "-W1", host],
                           capture_output=True, text=True, timeout=3)
        m = re.search(r"time=([\d.]+)", r.stdout)
        return (r.returncode == 0), (float(m.group(1)) if m else None)
    except Exception:
        return False, None


def tailscale_peers():
    if not any(os.access(os.path.join(p, "tailscale"), os.X_OK)
               for p in os.environ.get("PATH", "").split(":")):
        return None
    try:
        st = json.loads(subprocess.run(["tailscale", "status", "--json"],
                                       capture_output=True, text=True, timeout=5).stdout)
        peers = st.get("Peer") or {}
        return {"online": sum(1 for p in peers.values() if p.get("Online")),
                "total": len(peers)}
    except Exception:
        return None


def conntrack_count():
    try:
        with open("/proc/sys/net/netfilter/nf_conntrack_count") as f:
            return int(f.read())
    except OSError:
        return None


def detect_role(name, st, gw_dev, shares):
    if name in ROLE_OVERRIDES:
        kind, label = ROLE_OVERRIDES[name]
        return kind, (label or KIND_LABEL.get(kind, ""))
    if name == gw_dev:
        return "upstream", "upstream · default route"
    if name in shares:
        return "share", "shared subnet"
    if OVERLAY.match(name):
        return "overlay", "overlay"
    if st["wireless"]:
        return "uplink", "wifi"
    return "other", ""


class Slow(threading.Thread):
    """Lease/neighbour/upstream/tailscale refresh, off the 1 s sampling path."""
    daemon = True

    def __init__(self):
        super().__init__(name="gravenet-slow")
        self.data = {"upstream": None, "clients": [], "tailscale": None,
                     "thunderbolt": [], "shares": set()}

    def run(self):
        while True:
            try:
                addrs = addrs_by_iface()
                gw, gw_dev = default_gateway()
                ok, rtt = ping(gw) if gw else (False, None)
                neigh = neighbours()
                cl = [{**l, "neigh": neigh.get(l["ip"], {}).get("state", "—")}
                      for l in leases(addrs)]
                self.data = {
                    "upstream": {"gw": gw, "dev": gw_dev, "ok": ok, "rtt": rtt},
                    "clients": cl,
                    "tailscale": tailscale_peers(),
                    "thunderbolt": thunderbolt(),
                    "shares": lease_ifaces() | {c["iface"] for c in cl if c["iface"]},
                }
            except Exception:
                pass
            time.sleep(SLOW_EVERY)


class Sampler(threading.Thread):
    daemon = True

    def __init__(self, slow):
        super().__init__(name="gravenet-sampler")
        self.slow = slow
        self.history = deque(maxlen=HISTORY)
        self.clients = set()
        self.lock = threading.Lock()
        self.prev = None
        self.prev_t = None

    def run(self):
        host = socket.gethostname()
        while True:
            t = time.time()
            cnt = read_counters()
            addrs = addrs_by_iface()
            slow = self.slow.data
            gw_dev = (slow.get("upstream") or {}).get("dev")
            shares = slow.get("shares") or set()
            ifaces = {}
            for name in list_ifaces():
                if name not in cnt:
                    continue
                st = iface_static(name)
                kind, label = detect_role(name, st, gw_dev, shares)
                # unknown interfaces only earn a card while they are up
                if kind == "other" and st["state"] != "up":
                    continue
                rx_b, tx_b = cnt[name]
                rx_bps = tx_bps = 0.0
                if self.prev and name in self.prev and self.prev_t:
                    dt = max(t - self.prev_t, 0.25)
                    rx_bps = max(0.0, (rx_b - self.prev[name][0]) * 8 / dt)
                    tx_bps = max(0.0, (tx_b - self.prev[name][1]) * 8 / dt)
                ifaces[name] = {
                    "role": label, "kind": kind,
                    "state": st["state"], "speed": st["speed"],
                    "addrs": addrs.get(name, []),
                    "rx_bps": round(rx_bps), "tx_bps": round(tx_bps),
                    "rx_total": rx_b, "tx_total": tx_b,
                }
            self.prev, self.prev_t = cnt, t
            snap = {"t": round(t, 1), "host": host, "ifaces": ifaces,
                    "conntrack": conntrack_count(),
                    "upstream": slow.get("upstream"),
                    "clients": slow.get("clients") or [],
                    "tailscale": slow.get("tailscale"),
                    "thunderbolt": slow.get("thunderbolt") or []}
            with self.lock:
                self.history.append(snap)
                for q in list(self.clients):
                    try:
                        q.put_nowait(snap)
                    except Exception:
                        self.clients.discard(q)
            time.sleep(max(0.0, 1.0 - (time.time() - t)))


SLOW = Slow()
SAMPLER = Sampler(SLOW)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _body(self, code, ctype, body):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?")[0]
        if path.endswith("/events"):
            return self.sse()
        if path.endswith("/healthz"):
            return self._body(200, "text/plain", b"ok\n")
        try:
            with open(os.path.join(WEB_ROOT, "index.html"), "rb") as f:
                page = f.read()
        except OSError:
            self.send_error(404)
            return
        self._body(200, "text/html; charset=utf-8", page)

    def sse(self):
        import queue
        q = queue.Queue(maxsize=30)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        with SAMPLER.lock:
            hist = list(SAMPLER.history)
            SAMPLER.clients.add(q)
        try:
            self.wfile.write(b"event: history\ndata: " +
                             json.dumps(hist).encode() + b"\n\n")
            self.wfile.flush()
            while True:
                snap = q.get()
                self.wfile.write(b"data: " + json.dumps(snap).encode() + b"\n\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            with SAMPLER.lock:
                SAMPLER.clients.discard(q)


def main():
    SLOW.start()
    SAMPLER.start()
    srv = ThreadingHTTPServer((BIND, PORT), Handler)
    print(f"gravenet listening on {BIND}:{PORT}", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
