#!/usr/bin/env python3
# gravedash — status dashboard for a gravedecay appliance.
# Single file, stdlib only. Binds 127.0.0.1:$GRAVEDASH_PORT (gravedash.service),
# published tailnet-only via `tailscale serve`.
# Reads host state directly (systemd, docker, tmux -L agents, git, sensors,
# journald) — which is why this is a host service, not a container.
#
# Config via environment (set in gravedash.service / a drop-in):
#   GRAVE_ROOT                default /srv/dev
#   GRAVEDASH_PORT            default 4712
#   GRAVEDASH_ALLOWED_USERS   comma-separated Tailscale logins allowed to POST
#                             actions (empty = tailnet viewers are read-only;
#                             localhost is always trusted)
#   GRAVEDASH_UNITS           comma-separated systemd units to display

import functools
import glob
import io
import json
import os
import re
import shutil
import socket
import subprocess
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(os.environ.get("GRAVEDASH_PORT", "4712"))
GRAVE_ROOT = os.environ.get("GRAVE_ROOT", "/srv/dev")
ICON_PATH = os.environ.get("GRAVEDASH_ICON", os.path.join(GRAVE_ROOT, "config", "gravedecay.png"))
HOST = socket.gethostname()
# Tailscale serve injects Tailscale-User-Login for tailnet requests; POSTs
# (actions) are restricted to these identities. Requests with no header can
# only come from localhost (127.0.0.1 bind) and are trusted.
ALLOWED_USERS = set(filter(None, os.environ.get("GRAVEDASH_ALLOWED_USERS", "").split(",")))
UNITS = [u for u in os.environ.get(
    "GRAVEDASH_UNITS", "t3code,gravedash,tailscaled,sshd,docker").split(",") if u]
GRAVE = "/usr/local/bin/grave"
ACTIONS = {
    "gaming": ["sudo", "-n", GRAVE, "gaming"],
    "developer": ["sudo", "-n", GRAVE, "developer"],
    "restart-t3": ["sudo", "-n", "systemctl", "restart", "t3code"],
    "doctor": [GRAVE, "doctor"],
}
ANSI = re.compile(r"\x1b\[[0-9;]*m")


@functools.cache
def icon_png(size):
    """Home-screen icon from the installed gravedecay PNG. Never returns 404."""
    try:
        from PIL import Image
        with Image.open(ICON_PATH) as src:
            src = src.convert("RGB")
            src.thumbnail((size, size), Image.LANCZOS)
            img = Image.new("RGB", (size, size), "#000000")
            img.paste(src, ((size - src.width) // 2, (size - src.height) // 2))
    except Exception:
        try:
            with open(ICON_PATH, "rb") as f:
                return f.read()
        except OSError:
            pass
        from struct import pack
        import zlib
        row = b"\x00" + bytes.fromhex("000000") * size
        idat = zlib.compress(row * size)
        def chunk(tag, data):
            c = pack(">I", len(data)) + tag + data
            return c + pack(">I", zlib.crc32(tag + data))
        return (b"\x89PNG\r\n\x1a\n"
                + chunk(b"IHDR", pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0))
                + chunk(b"IDAT", idat) + chunk(b"IEND", b""))
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


MANIFEST = json.dumps({
    "name": "gravedash", "short_name": "gravedash", "start_url": "/",
    "display": "standalone", "background_color": "#0d0d0d", "theme_color": "#0d0d0d",
    "icons": [{"src": "/icon-192.png", "sizes": "192x192", "type": "image/png"},
              {"src": "/icon-512.png", "sizes": "512x512", "type": "image/png"}],
})


def sh(cmd, timeout=10):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout, p.stderr
    except Exception as e:
        return 1, "", str(e)


def unit_state(unit):
    rc, out, _ = sh(["systemctl", "show", unit, "-p", "ActiveState,SubState"])
    kv = dict(l.split("=", 1) for l in out.splitlines() if "=" in l)
    return {"unit": unit, "active": kv.get("ActiveState", "?"), "sub": kv.get("SubState", "?")}


def collect_services():
    return [unit_state(u) for u in UNITS]


def collect_docker():
    rc, out, err = sh(["docker", "ps", "-a", "--format",
                       "{{.Names}}\t{{.State}}\t{{.Status}}\t{{.Label \"com.docker.compose.project\"}}"])
    if rc != 0:
        return {"error": "docker unavailable (gaming mode?)", "containers": []}
    rows = []
    for line in out.splitlines():
        f = line.split("\t")
        if len(f) >= 3:
            rows.append({"name": f[0], "state": f[1], "status": f[2],
                         "project": f[3] if len(f) > 3 else ""})
    return {"error": None, "containers": sorted(rows, key=lambda r: (r["project"], r["name"]))}


def collect_tmux():
    rc, out, _ = sh(["tmux", "-L", "agents", "list-sessions", "-F",
                     "#{session_name}\t#{session_windows}\t#{?session_attached,attached,detached}\t#{t:session_activity}"])
    if rc != 0:
        return []
    rows = []
    for line in out.splitlines():
        f = line.split("\t")
        if len(f) >= 3:
            rows.append({"name": f[0], "windows": f[1], "attached": f[2],
                         "activity": f[3] if len(f) > 3 else ""})
    return rows


def collect_repos():
    repos = []
    base = f"{GRAVE_ROOT}/repos"
    try:
        entries = sorted(os.listdir(base))
    except OSError:
        return repos
    for name in entries:
        path = f"{base}/{name}"
        if not os.path.isdir(f"{path}/.git"):
            continue
        _, branch, _ = sh(["git", "-C", path, "rev-parse", "--abbrev-ref", "HEAD"])
        _, porcelain, _ = sh(["git", "-C", path, "status", "--porcelain"])
        _, last, _ = sh(["git", "-C", path, "log", "-1", "--format=%cr\t%s"])
        when, _, subject = last.strip().partition("\t")
        repos.append({"name": name, "branch": branch.strip(),
                      "dirty": len(porcelain.splitlines()),
                      "last_when": when, "last_subject": subject[:60]})
    return repos


def collect_journal():
    rc, out, _ = sh(["journalctl", "-q", "--system", "-p", "3", "-n", "12",
                     "--no-pager", "--since", "-24 hours", "-o", "short-iso"])
    if rc != 0:
        return ["(journal not readable)"]
    lines = [l for l in out.splitlines() if l.strip()]
    return lines or ["no errors in the last 24 h"]


def collect_temps():
    """Best-effort, vendor-agnostic: Intel coretemp or AMD k10temp for CPU,
    any amdgpu chip for GPU, any hwmon fans."""
    temps = {"cpu": None, "gpu": None, "gpu_mhz": None, "gpu_state": None, "fans": []}
    rc, out, _ = sh(["sensors", "-j"], timeout=5)
    if rc == 0:
        try:
            s = json.loads(out)
            for chip, feats in s.items():
                if not isinstance(feats, dict):
                    continue
                if temps["cpu"] is None and chip.startswith(("coretemp", "k10temp")):
                    for label in ("Package id 0", "Tctl", "Tdie"):
                        v = feats.get(label, {})
                        if isinstance(v, dict):
                            t = next((x for k, x in v.items() if k.endswith("_input")), None)
                            if t is not None:
                                temps["cpu"] = t
                                break
                if temps["gpu"] is None and chip.startswith("amdgpu"):
                    v = feats.get("edge", {}) or feats.get("junction", {})
                    if isinstance(v, dict):
                        temps["gpu"] = next((x for k, x in v.items() if k.endswith("_input")), None)
                for label, vals in feats.items():
                    if isinstance(vals, dict):
                        for k, v in vals.items():
                            if re.fullmatch(r"fan\d+_input", k) and v:
                                temps["fans"].append(round(v))
            temps["fans"] = temps["fans"][:4]
        except (ValueError, TypeError):
            pass
    # If a host profile pins the GPU DPM level, surface the pinned sclk; the
    # table reads "0Mhz *" while runtime-suspended — report that as state.
    # (glob "card*" would also match connector dirs like card1-DP-1)
    for path in glob.glob("/sys/class/drm/card*/device/pp_dpm_sclk"):
        dev = os.path.dirname(path)
        try:
            with open(f"{dev}/power/runtime_status") as f:
                temps["gpu_state"] = f.read().strip()
            with open(path) as f:
                for line in f:
                    if "*" in line:
                        m = re.search(r"(\d+)\s*[Mm]hz", line)
                        if m and int(m.group(1)) > 0:
                            temps["gpu_mhz"] = int(m.group(1))
        except OSError:
            pass
    return temps


def collect_system():
    with open("/proc/loadavg") as f:
        load1, load5, load15 = f.read().split()[:3]
    mem = {}
    with open("/proc/meminfo") as f:
        for line in f:
            k, v = line.split(":", 1)
            mem[k] = int(v.split()[0])  # kB
    with open("/proc/uptime") as f:
        uptime = float(f.read().split()[0])
    # / and GRAVE_ROOT may be subvolumes of one pool — dedupe by source device
    disks, seen = [], set()
    for label, path in (("/", "/"), (GRAVE_ROOT, GRAVE_ROOT)):
        rc, src, _ = sh(["findmnt", "-n", "-o", "SOURCE", path])
        dev = src.strip().split("[")[0] or path
        if dev in seen:
            continue
        seen.add(dev)
        u = shutil.disk_usage(path)
        disks.append({"label": label, "total": u.total, "used": u.used,
                      "pct": round(u.used / u.total * 100, 1)})
    if len(disks) == 1:
        disks[0]["label"] = f"/ + {GRAVE_ROOT}"
    mem_total = mem.get("MemTotal", 1)
    mem_avail = mem.get("MemAvailable", 0)
    return {
        "load": [float(load1), float(load5), float(load15)],
        "ncpu": os.cpu_count(),
        "mem": {"total_kb": mem_total, "used_kb": mem_total - mem_avail,
                "pct": round((mem_total - mem_avail) / mem_total * 100, 1)},
        "disks": disks, "uptime_s": int(uptime), "temps": collect_temps(),
    }


def collect_backups():
    base = f"{GRAVE_ROOT}/backups"
    try:
        entries = sorted(d for d in os.listdir(base) if os.path.isdir(f"{base}/{d}"))
    except OSError:
        entries = []
    return {"count": len(entries), "latest": entries[-1] if entries else None}


def state(headers):
    t3 = unit_state("t3code")
    mode = "developer" if t3["active"] == "active" else "gaming"
    return {
        "host": HOST,
        "now": time.strftime("%H:%M:%S"),
        "viewer": headers.get("Tailscale-User-Login", "local"),
        "mode": mode,
        "services": collect_services(),
        "docker": collect_docker(),
        "tmux": collect_tmux(),
        "repos": collect_repos(),
        "journal": collect_journal(),
        "system": collect_system(),
        "backups": collect_backups(),
    }


class Handler(BaseHTTPRequestHandler):
    server_version = "gravedash/1"

    def log_message(self, fmt, *args):  # journald gets enough from systemd
        pass

    def _send(self, code, body, ctype="application/json"):
        data = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/healthz":
            self._send(200, '{"ok":true}')
        elif self.path == "/api/state":
            self._send(200, json.dumps(state(self.headers)))
        elif self.path == "/":
            boot = json.dumps(state(self.headers)).replace("</", "<\\/")
            self._send(200, PAGE.replace("/*BOOT*/null", boot), "text/html; charset=utf-8")
        elif self.path == "/manifest.webmanifest":
            self._send(200, MANIFEST, "application/manifest+json")
        elif self.path in ("/apple-touch-icon.png", "/icon-180.png"):
            self._send(200, icon_png(180), "image/png")
        elif self.path == "/icon-192.png":
            self._send(200, icon_png(192), "image/png")
        elif self.path == "/icon-512.png":
            self._send(200, icon_png(512), "image/png")
        else:
            self._send(404, '{"error":"not found"}')

    def do_POST(self):
        viewer = self.headers.get("Tailscale-User-Login")
        if viewer is not None and viewer not in ALLOWED_USERS:
            self._send(403, json.dumps({
                "ok": False,
                "output": f"forbidden for {viewer} — add to GRAVEDASH_ALLOWED_USERS"}))
            return
        if self.path != "/api/action":
            self._send(404, '{"error":"not found"}')
            return
        try:
            length = int(self.headers.get("Content-Length", 0))
            action = json.loads(self.rfile.read(length))["action"]
            cmd = ACTIONS[action]
        except (ValueError, KeyError):
            self._send(400, json.dumps({"ok": False, "output": "unknown action"}))
            return
        rc, out, err = sh(cmd, timeout=120)
        self._send(200, json.dumps({"ok": rc == 0, "output": ANSI.sub("", out + err)}))


PAGE = r"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="theme-color" content="#0d0d0d">
<meta name="mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="apple-mobile-web-app-title" content="gravedash">
<link rel="manifest" href="/manifest.webmanifest">
<link rel="apple-touch-icon" href="/apple-touch-icon.png">
<link rel="icon" type="image/png" href="/icon-192.png">
<title>gravedash · @HOST@</title>
<style>
:root{
  --page:#0d0d0d; --surface:#1a1a19; --ink:#ffffff; --ink-2:#c3c2b7;
  --muted:#898781; --hairline:#2c2c2a; --ring:rgba(255,255,255,.10);
  --accent:#3987e5; --accent-soft:#6da7ec;
  --good:#0ca30c; --warn:#fab219; --crit:#d03b3b;
  --track-blue:#17324f; --track-warn:#453208; --track-crit:#431616;
}
*{box-sizing:border-box;margin:0;-webkit-tap-highlight-color:transparent}
html{-webkit-text-size-adjust:100%}
body{background:var(--page);color:var(--ink-2);
  font:14px/1.45 system-ui,-apple-system,"Segoe UI",sans-serif;
  padding:0 16px calc(24px + env(safe-area-inset-bottom));max-width:1120px;margin:0 auto}
h1{font-size:17px;font-weight:600;color:var(--ink)}
.topbar{position:sticky;top:0;z-index:10;display:flex;flex-wrap:wrap;gap:10px;align-items:center;
  background:rgba(13,13,13,.88);backdrop-filter:blur(12px);-webkit-backdrop-filter:blur(12px);
  margin:0 -16px 12px;padding:calc(10px + env(safe-area-inset-top)) 16px 10px;
  border-bottom:1px solid var(--hairline)}
.topbar .meta{color:var(--muted);font-size:12px;margin-left:auto}
.badge{display:inline-flex;align-items:center;gap:6px;padding:3px 10px;border-radius:99px;
  border:1px solid var(--ring);font-size:12px;font-weight:600;color:var(--ink)}
.actions{display:flex;flex-wrap:wrap;gap:8px;margin-bottom:14px}
@media(max-width:640px){.actions{display:grid;grid-template-columns:1fr 1fr}}
button{background:var(--surface);color:var(--ink);border:1px solid var(--ring);
  border-radius:10px;padding:10px 16px;min-height:46px;font:600 14px system-ui,sans-serif;
  cursor:pointer;touch-action:manipulation}
button:hover{border-color:var(--accent)}
button:active{transform:scale(.97)}
button:disabled{opacity:.45;cursor:default;transform:none}
button.busy{opacity:.6;cursor:wait}
@media(pointer:coarse){td{padding-top:9px;padding-bottom:9px}}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px;margin-bottom:14px}
.tile{background:var(--surface);border:1px solid var(--ring);border-radius:10px;padding:12px 14px}
.tile .label{font-size:12px;color:var(--muted);margin-bottom:4px}
.tile .value{font-size:25px;font-weight:600;color:var(--ink)}
.tile .sub{font-size:12px;color:var(--muted);margin-top:2px}
.meter{height:6px;border-radius:3px;margin-top:8px;background:var(--track-blue);overflow:hidden}
.meter i{display:block;height:100%;border-radius:3px;background:var(--accent)}
.meter.warn{background:var(--track-warn)} .meter.warn i{background:var(--warn)}
.meter.crit{background:var(--track-crit)} .meter.crit i{background:var(--crit)}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-bottom:10px}
@media(max-width:760px){.grid{grid-template-columns:1fr}}
.panel{background:var(--surface);border:1px solid var(--ring);border-radius:10px;padding:12px 14px}
.panel h2{font-size:12px;font-weight:600;color:var(--muted);text-transform:uppercase;
  letter-spacing:.05em;margin-bottom:8px}
table{width:100%;border-collapse:collapse;font-size:13px}
td{padding:4px 8px 4px 0;border-top:1px solid var(--hairline);vertical-align:top}
tr:first-child td{border-top:none}
td.num{text-align:right;font-variant-numeric:tabular-nums;color:var(--ink-2)}
td.dim{color:var(--muted)}
.dot{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:7px;
  border:2px solid var(--surface);box-sizing:content-box;vertical-align:-1px}
.st-good{background:var(--good)} .st-warn{background:var(--warn)} .st-crit{background:var(--crit)}
pre{background:var(--page);border:1px solid var(--hairline);border-radius:8px;padding:10px;
  font:12px/1.5 ui-monospace,monospace;overflow-x:auto;white-space:pre-wrap;color:var(--ink-2)}
.spark{display:block;margin-top:6px}
#out-panel{display:none;margin-bottom:14px}
.full{margin-bottom:10px}
a{color:var(--accent-soft)}
</style></head><body>
<div class="topbar">
  <h1>gravedash</h1>
  <span class="badge" id="mode">…</span>
  <span class="meta" id="meta">connecting…</span>
</div>
<div class="actions">
  <button data-act="gaming" data-confirm="Stop dev services and free RAM for gaming?">🎮 Gaming mode</button>
  <button data-act="developer" data-confirm="Start all developer services?">💻 Developer mode</button>
  <button data-act="restart-t3" data-confirm="Restart T3 Code? Active agent sessions survive, the UI reconnects.">↻ Restart T3 Code</button>
  <button data-act="doctor">🩺 Run doctor</button>
</div>
<div class="panel" id="out-panel"><h2 id="out-title">output</h2><pre id="out"></pre></div>
<div class="tiles" id="tiles"></div>
<div class="grid">
  <div class="panel"><h2>⚙️ Services</h2><table id="services"></table></div>
  <div class="panel"><h2>🐳 Docker</h2><table id="docker"></table></div>
  <div class="panel"><h2>🤖 Agent sessions (tmux)</h2><table id="tmux"></table></div>
  <div class="panel"><h2>📦 Repos</h2><table id="repos"></table></div>
</div>
<div class="panel full"><h2>📋 Journal errors (24 h)</h2><pre id="journal"></pre></div>
<script>
const $=id=>document.getElementById(id);
const esc=s=>String(s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const hist={load:[],cpu:[]};   // client-side sparkline history (last 60 polls)
function push(k,v){if(v==null)return;hist[k].push(v);if(hist[k].length>60)hist[k].shift();}
function spark(k){
  const d=hist[k]; if(d.length<2) return '';
  const w=120,h=26,min=Math.min(...d),max=Math.max(...d),span=(max-min)||1;
  const pts=d.map((v,i)=>[(i/(d.length-1))*(w-6)+3,h-3-((v-min)/span)*(h-6)]);
  const path=pts.map(p=>p.join(',')).join(' ');
  const[ex,ey]=pts[pts.length-1];
  return `<svg class="spark" width="${w}" height="${h}" aria-hidden="true">
    <polyline points="${path}" fill="none" stroke="var(--accent-soft)" stroke-width="2"
      stroke-linejoin="round" stroke-linecap="round" opacity=".75"/>
    <circle cx="${ex}" cy="${ey}" r="4" fill="var(--accent)" stroke="var(--surface)" stroke-width="2"/>
  </svg>`;
}
function meterClass(p){return p>92?'crit':p>80?'warn':''}
function meter(p){return `<div class="meter ${meterClass(p)}"><i style="width:${Math.min(p,100)}%"></i></div>`}
function tile(label,value,sub,extra){return `<div class="tile"><div class="label">${label}</div>
  <div class="value">${value}</div>${sub?`<div class="sub">${sub}</div>`:''}${extra||''}</div>`}
function fmtGB(kb){return (kb/1048576).toFixed(1)+' GB'}
function fmtUp(s){const d=Math.floor(s/86400),h=Math.floor(s%86400/3600),m=Math.floor(s%3600/60);
  return d?`${d}d ${h}h`:h?`${h}h ${m}m`:`${m}m`}
function statusDot(state){
  const cls=state==='active'?'st-good':(state==='inactive'?'st-warn':'st-crit');
  return `<span class="dot ${cls}"></span>`;
}
function render(s){
  $('mode').textContent=(s.mode==='developer'?'💻 developer':'🎮 gaming');
  $('meta').textContent=`${s.viewer} · up ${fmtUp(s.system.uptime_s)} · ${s.now}`;
  // the mode you're already in isn't a button you can press
  document.querySelector('[data-act="gaming"]').disabled=(s.mode==='gaming');
  document.querySelector('[data-act="developer"]').disabled=(s.mode==='developer');
  const sys=s.system,t=sys.temps;
  push('load',sys.load[0]); push('cpu',t.cpu);
  $('tiles').innerHTML=
    tile('Load (1 m)',sys.load[0].toFixed(2),`${sys.load[1].toFixed(2)} / ${sys.load[2].toFixed(2)} · ${sys.ncpu} cores`,spark('load'))+
    tile('CPU temp',t.cpu!=null?Math.round(t.cpu)+'°':'—','package',spark('cpu'))+
    tile('GPU temp',t.gpu!=null?Math.round(t.gpu)+'°':'—',
      t.gpu_mhz?`sclk ${t.gpu_mhz} MHz`:(t.gpu_state==='suspended'?'runtime suspended':esc(t.gpu_state||'')))+
    tile('Fans',t.fans.length?t.fans.map(f=>f).join(' / '):'—','rpm')+
    tile('Memory',sys.mem.pct+'%',`${fmtGB(sys.mem.used_kb)} of ${fmtGB(sys.mem.total_kb)}`,meter(sys.mem.pct))+
    sys.disks.map(d=>tile('Disk '+esc(d.label),d.pct+'%',
      `${fmtGB(d.used/1024)} used · ${s.backups.count} backups`,meter(d.pct))).join('');
  $('services').innerHTML=s.services.map(u=>`<tr>
    <td>${statusDot(u.active)}${esc(u.unit)}</td>
    <td class="dim">${esc(u.active)} (${esc(u.sub)})</td></tr>`).join('');
  $('docker').innerHTML=s.docker.error
    ? `<tr><td class="dim">${esc(s.docker.error)}</td></tr>`
    : (s.docker.containers.map(c=>`<tr>
        <td>${statusDot(c.state==='running'?'active':'failed')}${esc(c.name)}</td>
        <td class="dim">${esc(c.status)}</td></tr>`).join('')||'<tr><td class="dim">no containers</td></tr>');
  $('tmux').innerHTML=s.tmux.length?s.tmux.map(x=>`<tr>
      <td>${statusDot('active')}${esc(x.name)}</td>
      <td class="num">${esc(x.windows)} win</td>
      <td class="dim">${esc(x.attached)}</td></tr>`).join('')
    :'<tr><td class="dim">no agent sessions — <code>grave agents new</code></td></tr>';
  $('repos').innerHTML=s.repos.length?s.repos.map(r=>`<tr>
      <td>${statusDot(r.dirty?'inactive':'active')}${esc(r.name)}</td>
      <td class="dim">${esc(r.branch)}${r.dirty?` · ${r.dirty} dirty`:''}</td>
      <td class="dim">${esc(r.last_when)}</td></tr>`).join('')
    :'<tr><td class="dim">no repos</td></tr>';
  $('journal').textContent=s.journal.join('\n');
}
async function poll(){
  if(document.hidden)return;
  try{
    const r=await fetch('/api/state');
    render(await r.json());
  }catch(e){ $('meta').textContent='unreachable — retrying'; }
}
document.querySelectorAll('button[data-act]').forEach(b=>b.onclick=async()=>{
  const act=b.dataset.act;
  if(b.dataset.confirm&&!confirm(b.dataset.confirm))return;
  b.classList.add('busy');
  $('out-panel').style.display='block';
  $('out-title').textContent=act; $('out').textContent='running…';
  try{
    const r=await fetch('/api/action',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({action:act})});
    const j=await r.json();
    $('out').textContent=(j.ok?'':'FAILED\n')+(j.output||'(no output)');
  }catch(e){ $('out').textContent='request failed: '+e; }
  b.classList.remove('busy'); poll();
});
const BOOT=/*BOOT*/null;   // server-rendered initial state: instant first paint
if(BOOT)render(BOOT);else poll();
setInterval(poll,5000);
document.addEventListener('visibilitychange',()=>{if(!document.hidden)poll()});
</script></body></html>
""".replace("@HOST@", HOST)

if __name__ == "__main__":
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
