#!/usr/bin/env python3
# gravedecay — status dashboard for a gravedecay appliance.
# Single file, stdlib only. Binds 127.0.0.1:$GRAVEDECAY_PORT (gravedecay.service),
# published tailnet-only via `tailscale serve`.
# Reads host state directly (systemd, docker, tmux -L agents, git, sensors,
# journald) — which is why this is a host service, not a container.
#
# Config via environment (set in gravedecay.service / a drop-in):
#   GRAVE_ROOT                default /srv/dev
#   GRAVEDECAY_PORT            default 4712
#   GRAVEDECAY_ALLOWED_USERS   comma-separated Tailscale logins allowed to POST
#                             actions (empty = tailnet viewers are read-only;
#                             localhost is always trusted)
#   GRAVEDECAY_UNITS           comma-separated systemd units to display
#   GRAVEDECAY_APPS            launcher tiles, "label=url;label=url".
#                             gravedecay is the appliance's single entry point:
#                             every app you mount (T3, future ones) gets a tile.

import functools
import glob
import hashlib
import io
import json
import os
import re
import shutil
import socket
import subprocess
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(os.environ.get("GRAVEDECAY_PORT", os.environ.get("DASH_PORT", "4712")))
GRAVE_ROOT = os.environ.get("GRAVE_ROOT", "/srv/dev")
# tmux socket carrying the agent sessions. Single-user uses "agents"; a workspace
# dashboard is handed its per-workspace socket (grave-<slug>) via TMUX_SOCKET so
# the sessions panel and kill button see the same sessions the workspace terminal
# creates. MUST match TMUX_SOCKET on the matching gravedecay-term unit.
TMUX_SOCKET = os.environ.get("TMUX_SOCKET", "agents")
# Where the T3 instance this dashboard drives keeps its pairing state. Single-user
# T3 serves from $GRAVE_ROOT/agents/t3code; a workspace dashboard is handed its
# per-workspace state dir (state/t3) so minted tokens land where its T3 reads them.
T3_BASE_DIR = os.environ.get("GRAVEDECAY_T3_BASE_DIR", f"{GRAVE_ROOT}/agents/t3code")
# Mount prefix when path-routed behind `tailscale serve --set-path` on the same
# origin as T3 (single entry point). Bare paths keep working for localhost.
BASE = os.environ.get("GRAVEDECAY_BASE", "/grave").rstrip("/")
ICON_PATH = os.environ.get("GRAVEDECAY_ICON", os.path.join(GRAVE_ROOT, "config", "gravedecay.png"))
HOST = socket.gethostname()
with open(__file__, "rb") as _source:
    BUILD_ID = hashlib.sha256(_source.read()).hexdigest()
# File manager: browse / upload / edit files from the browser, as a modal in
# the dashboard. Confined to the appliance root — every request path is
# realpath'd and prefix-checked against FILES_ROOT (see _safe_path), so `..`
# and symlinks that escape the tree are refused. Reads are gated to
# ALLOWED_USERS exactly like writes: listing the filesystem is sensitive.
FILES_ROOT = os.path.realpath(GRAVE_ROOT)
# The appliance's OWN secret store (600-mode .env files systemd reads:
# Linear key, t3.env, …) is hidden from the file manager even though it sits
# inside the jail — a browser button that can read or overwrite these is a
# footgun. This is a path guard, NOT a "*.env" blanket: repo .env files under
# repos/ stay fully editable, since projects get copied across boxes.
FILES_DENY = (os.path.join(FILES_ROOT, "config", "secrets"),)
MAX_UPLOAD = 2 * 1024 * 1024 * 1024   # 2 GiB per uploaded file
# Tailscale serve injects Tailscale-User-Login for tailnet requests; POSTs
# (actions) are restricted to these identities. Requests with no header can
# only come from localhost (127.0.0.1 bind) and are trusted.
ALLOWED_USERS = set(filter(None, os.environ.get("GRAVEDECAY_ALLOWED_USERS", "").split(",")))
UNITS = [u for u in os.environ.get(
    "GRAVEDECAY_UNITS", "t3code,gravedecay,gravedecay-term,tailscaled,sshd,docker").split(",") if u]
APPS = [{"name": name.strip(), "url": url.strip()}
        for name, _, url in (p.partition("=") for p in os.environ.get(
            "GRAVEDECAY_APPS", "⌨️ T3 Code=/").split(";"))
        if url.strip()]
# User preferences, editable from the ⚙️ panel (writes gated to ALLOWED_USERS
# exactly like actions). Stored beside the other appliance config.
SETTINGS_PATH = os.path.join(GRAVE_ROOT, "config", "gravedecay-settings.json")
DEFAULT_SETTINGS = {
    "panel_order": ["prs", "linear", "ci", "tmux", "usage", "repos",
                    "stats", "actions", "services", "docker", "journal"],
    "hidden_panels": [],   # panel ids to hide
    "hidden_apps": [],     # launcher tile names to hide
    "newtab_apps": [],     # tile names that open in a new tab instead of in-PWA
    "modal_apps": [],      # tile names that open in an iframe modal on the dashboard
    "yolo_apps": [],       # claude/codex tiles launched with permission gates OFF
    "custom_apps": [],     # extra tiles: [{"name": ..., "url": ...}]
    "poll_ms": 5000,       # dashboard refresh interval
}


def _safe_tile_url(url):
    """Return url only if it is an internal root-relative path or an http(s) URL.
    Everything else — javascript:, data:, etc. — is dropped: a custom tile is
    rendered as an <a href> and an iframe src, so a javascript: URL would run in
    the dashboard origin (stored XSS driving every privileged endpoint)."""
    url = str(url)[:200]
    if url.startswith("/"):
        return url
    try:
        scheme = urllib.parse.urlparse(url).scheme.lower()
    except ValueError:
        return ""
    return url if scheme in ("http", "https") else ""


def _sanitize_custom_apps(apps):
    out = []
    for a in apps if isinstance(apps, list) else []:
        if not isinstance(a, dict) or not a.get("url"):
            continue
        url = _safe_tile_url(a.get("url", ""))
        if url:
            out.append({"name": str(a.get("name", "app"))[:40], "url": url})
    return out[:12]


def load_settings():
    try:
        with open(SETTINGS_PATH) as f:
            data = json.load(f)
    except (OSError, ValueError):
        data = {}
    s = dict(DEFAULT_SETTINGS)
    for k, default in DEFAULT_SETTINGS.items():
        if k in data and isinstance(data[k], type(default)):
            s[k] = data[k]
    s["poll_ms"] = max(2000, min(60000, int(s["poll_ms"])))
    s["custom_apps"] = _sanitize_custom_apps(s["custom_apps"])  # neutralize a poisoned file
    return s


def save_settings(data):
    merged = load_settings()
    for k, default in DEFAULT_SETTINGS.items():
        if k in data and isinstance(data[k], type(default)):
            merged[k] = data[k]
    merged["poll_ms"] = max(2000, min(60000, int(merged["poll_ms"])))
    merged["custom_apps"] = _sanitize_custom_apps(merged["custom_apps"])
    tmp = SETTINGS_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(merged, f, indent=2)
    os.replace(tmp, SETTINGS_PATH)
    return merged


GRAVE = os.environ.get("GRAVEDECAY_GRAVE", "/usr/local/bin/grave")
# t3 lives in the same durable-toolchain bin dir as grave (~/.local/bin on an
# immutable rootfs). systemd's minimal PATH omits it, so resolve it absolutely
# like GRAVE — deriving from GRAVE's dir keeps package-host / SteamOS parity.
T3 = os.environ.get("GRAVEDECAY_T3", os.path.join(os.path.dirname(GRAVE), "t3"))
# grave runs AS THE SERVICE USER (it sudo -n's internally where needed):
# under sudo it would be root, whose tmux lives in /tmp/tmux-0 — freeze/kill
# of agent sessions would silently no-op.
ACTIONS = {
    "gaming": [GRAVE, "gaming"],                 # 🧊 freeze sessions
    "gaming-kill": [GRAVE, "gaming", "--kill"],  # ☠️ destroy them
    "developer": [GRAVE, "developer"],
    "restart-t3": ["sudo", "-n", "systemctl", "restart", "t3code"],
    "update-t3": [GRAVE, "t3", "update"],
    # Detached system unit: `grave upgrade` re-raises and restarts this
    # dashboard, so it must not run inside gravedecay.service's cgroup.
    "update-grave": ["sudo", "-n", "systemctl", "--no-block", "start",
                     "gravedecay-upgrade.service"],
    "doctor": [GRAVE, "doctor"],
    # one-time device pairing token for T3 (viewer-gated like everything
    # else); --base-url is appended per-request from the Host header so the
    # printed /pair#token=... link lands on the right origin
    "t3-pair": [T3, "auth", "pairing", "create",
                "--base-dir", T3_BASE_DIR,
                "--ttl", "15m", "--label", "gravedecay-dashboard"],
    "reboot": ["sudo", "-n", "systemctl", "reboot"],
    "bootmode-developer": [GRAVE, "bootmode", "developer"],
    "bootmode-gaming": [GRAVE, "bootmode", "gaming"],
    "gamewatch-on": [GRAVE, "gamewatch", "on"],    # auto-throttle: freeze on game launch
    "gamewatch-off": [GRAVE, "gamewatch", "off"],
}
ANSI = re.compile(r"\x1b\[[0-9;]*m")
# Only one grave action at a time: concurrent mode flips race each other
# (instrumentation caught a developer run failing mid gaming-kill).
ACTION_LOCK = threading.Lock()


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


# Relative URLs throughout so the app works both bare (127.0.0.1:4712/) and
# mounted (https://box/grave/) without caring which.
MANIFEST = json.dumps({
    # The installed app is the front door for the whole appliance origin:
    # /grave/ (dashboard), / (T3), /term/ and /pair/.  A /grave/-only scope
    # makes those launcher destinations leave the standalone app in standards-
    # compliant browsers.  id remains stable across manifest revisions.
    "id": f"{BASE or '/grave'}/", "name": "gravedecay", "short_name": "gravedecay",
    "start_url": "./", "scope": "/",
    "display": "standalone", "background_color": "#070907", "theme_color": "#070907",
    "icons": [{"src": "icon-192.png", "sizes": "192x192", "type": "image/png"},
              {"src": "icon-512.png", "sizes": "512x512", "type": "image/png"}],
})

# Network-first navigation only.  The dashboard is a remote control, so stale
# API/system data must never be cached or presented as live.  The tiny offline
# document merely keeps an installed Safari web app useful enough to explain
# that the box/Tailscale is unreachable and retry without dropping to a blank
# WebKit error page.  The worker is allowed to cover the entire origin because
# the manifest intentionally does too; non-navigation requests pass through.
SERVICE_WORKER = r"""const CACHE='gravedecay-shell-v1';
const OFFLINE=new URL('offline.html',self.location.href).href;
self.addEventListener('install',event=>{
  event.waitUntil(caches.open(CACHE).then(cache=>cache.add(OFFLINE)).then(()=>self.skipWaiting()));
});
self.addEventListener('activate',event=>{
  event.waitUntil(caches.keys().then(keys=>Promise.all(
    keys.filter(key=>key!==CACHE).map(key=>caches.delete(key)))).then(()=>self.clients.claim()));
});
self.addEventListener('fetch',event=>{
  if(event.request.mode!=='navigate')return;
  event.respondWith(fetch(event.request).catch(()=>caches.match(OFFLINE)));
});
"""

OFFLINE_PAGE = r"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="theme-color" content="#070907"><title>gravedecay · offline</title>
<style>*{box-sizing:border-box}body{margin:0;min-height:100dvh;display:grid;place-items:center;
padding:max(24px,env(safe-area-inset-top)) max(18px,env(safe-area-inset-right))
max(24px,env(safe-area-inset-bottom)) max(18px,env(safe-area-inset-left));background:#070907;
color:#a8e6a3;font:15px/1.6 ui-monospace,Menlo,monospace}.box{width:min(34rem,100%);
border:1px solid #2e4a2e;padding:22px}h1{margin:0 0 10px;color:#d6ffd0;font-size:20px}
p{margin:8px 0;color:#557a55}button{margin-top:12px;min-height:44px;padding:8px 16px;
border:1px solid #2e4a2e;background:transparent;color:#d6ffd0;font:700 14px ui-monospace,Menlo,monospace}
</style></head><body><main class="box"><h1>🪦 gravedecay is unreachable</h1>
<p>The dashboard needs a live path to the box. Check that Tailscale is connected and the machine is awake.</p>
<button onclick="location.reload()">↻ retry connection</button></main></body></html>"""


def static_asset(name, fallback):
    """Read source-tree or installed dashboard assets, with an embedded
    fallback so an interrupted/older upgrade never makes the UI unbootable."""
    here = os.path.dirname(os.path.abspath(__file__))
    for directory in (os.path.join(here, "static"),
                      os.path.join(here, "dashboard-static")):
        try:
            with open(os.path.join(directory, name), encoding="utf-8") as f:
                return f.read()
        except OSError:
            pass
    return fallback


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
    rc, out, _ = sh(["tmux", "-L", TMUX_SOCKET, "list-sessions", "-F",
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
    # /api/state polls as fast as every 2 s and this forks 3 git processes PER
    # repo; without a TTL cache a few read-only viewers (or a many-repo box)
    # saturate CPU/PIDs. Cache like the github/ci/linear collectors do.
    def fetch():
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
    return cached("repos", 15, fetch)


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
    # one disk tile: / covers the whole pool (GRAVE_ROOT is a subvolume of it
    # on btrfs setups; a separate tile for it was redundant noise)
    u = shutil.disk_usage("/")
    disks = [{"label": "/", "total": u.total, "used": u.used,
              "pct": round(u.used / u.total * 100, 1)}]
    mem_total = mem.get("MemTotal", 1)
    mem_avail = mem.get("MemAvailable", 0)
    return {
        "load": [float(load1), float(load5), float(load15)],
        "ncpu": os.cpu_count(),
        "mem": {"total_kb": mem_total, "used_kb": mem_total - mem_avail,
                "pct": round((mem_total - mem_avail) / mem_total * 100, 1)},
        "disks": disks, "uptime_s": int(uptime), "temps": collect_temps(),
    }


# Remote integrations poll on a slow TTL so the 5 s dashboard refresh never
# hammers the GitHub/Linear APIs.
_ttl_cache = {}


def cached(key, ttl, fn):
    now = time.monotonic()
    hit = _ttl_cache.get(key)
    if hit and now - hit[0] < ttl:
        return hit[1]
    val = fn()
    _ttl_cache[key] = (now, val)
    return val


def collect_github():
    def fetch():
        rc, out, _ = sh(["gh", "api", "user", "--jq", ".login"], timeout=10)
        login = out.strip() if rc == 0 and out.strip() else None
        if not login:
            return {"login": None, "prs": [],
                    "error": "gh not authenticated — ⚙️ settings → Re-auth GitHub"}
        def search(*extra):
            rc, out, _ = sh(["gh", "search", "prs", "--state=open", *extra,
                             "--json", "number,title,repository,url"], timeout=15)
            rows = []
            if rc == 0:
                try:
                    for p in json.loads(out):
                        repo = (p.get("repository") or {}).get("nameWithOwner", "?")
                        rows.append({"repo": repo.split("/")[-1], "number": p.get("number"),
                                     "title": str(p.get("title", ""))[:80], "url": p.get("url", "")})
                except ValueError:
                    pass
            return rows[:15]
        more = (f"https://github.com/search?q=owner%3A{login}+is%3Apr+is%3Aopen"
                "&type=pullrequests")
        # one merged list: my repos' open PRs, flagged 👀 where my review is
        # requested — plus review requests from OTHER people's repos on top
        reviews = search(f"--review-requested={login}")
        rurls = {r["url"] for r in reviews}
        prs = search("--owner", login)
        purls = {p["url"] for p in prs}
        for p in prs:
            p["mine"] = p["url"] in rurls
        prs = [dict(r, mine=True) for r in reviews if r["url"] not in purls] + prs
        return {"login": login, "error": None, "prs": prs[:15], "more_url": more}
    return cached("github", 120, fetch)


def collect_ci():
    """Latest workflow run per repo under $GRAVE_ROOT/repos with a GitHub remote."""
    def fetch():
        rows = []
        base = f"{GRAVE_ROOT}/repos"
        try:
            entries = sorted(os.listdir(base))
        except OSError:
            entries = []
        for name in entries[:12]:
            path = f"{base}/{name}"
            if not os.path.isdir(f"{path}/.git"):
                continue
            rc, out, _ = sh(["git", "-C", path, "remote", "get-url", "origin"])
            m = re.search(r"github\.com[:/]([^/\s]+/[^/.\s]+)", out)
            if rc != 0 or not m:
                continue
            rc, out, _ = sh(["gh", "run", "list", "-R", m.group(1), "-L", "1",
                             "--json", "workflowName,conclusion,status,url,headBranch"],
                            timeout=15)
            try:
                runs = json.loads(out) if rc == 0 else []
            except ValueError:
                runs = []
            if runs:
                r = runs[0]
                rows.append({"repo": name, "workflow": r.get("workflowName", ""),
                             "branch": r.get("headBranch", ""), "status": r.get("status", ""),
                             "conclusion": r.get("conclusion") or "", "url": r.get("url", "")})
        return {"rows": rows}
    return cached("ci", 180, fetch)


LINEAR_ENV = os.path.join(GRAVE_ROOT, "config", "secrets", "linear.env")


def linear_key():
    try:
        with open(LINEAR_ENV) as f:
            for line in f:
                if line.startswith("LINEAR_API_KEY="):
                    return line.split("=", 1)[1].strip()
    except OSError:
        pass
    return None


def collect_linear():
    def fetch():
        key = linear_key()
        if not key:
            return {"configured": False, "issues": [], "error": None}
        query = json.dumps({"query": """{ organization { urlKey }
          viewer { assignedIssues(
            first: 15, filter: {state: {type: {nin: ["completed", "canceled"]}}}
        ) { nodes { identifier title url state { name } } } } }"""})
        try:
            req = urllib.request.Request(
                "https://api.linear.app/graphql", data=query.encode(),
                headers={"Authorization": key, "Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=10) as r:
                data = json.load(r)["data"]
            nodes = data["viewer"]["assignedIssues"]["nodes"]
            slug = (data.get("organization") or {}).get("urlKey")
        except Exception as e:
            return {"configured": True, "issues": [], "error": f"linear: {e}"}
        return {"configured": True, "error": None,
                "more_url": f"https://linear.app/{slug}/my-issues" if slug else "https://linear.app",
                "issues": [
            {"id": n["identifier"], "title": n["title"][:80], "url": n["url"],
             "state": (n.get("state") or {}).get("name", "")} for n in nodes]}
    return cached("linear", 120, fetch)


def linear_gql(payload):
    req = urllib.request.Request(
        "https://api.linear.app/graphql", data=json.dumps(payload).encode(),
        headers={"Authorization": linear_key() or "", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.load(r)["data"]


def linear_meta():
    """Viewer id + default (first) team, for quick-create. Cached an hour."""
    def fetch():
        if not linear_key():
            return None
        try:
            v = linear_gql({"query":
                "{ viewer { id teams(first: 1) { nodes { id key } } } }"})["viewer"]
            return {"viewer_id": v["id"], "team_id": v["teams"]["nodes"][0]["id"]}
        except Exception:
            return None
    return cached("linear-meta", 3600, fetch)


def linear_create(title):
    meta = linear_meta()
    if not meta:
        return {"ok": False, "output": "linear not configured"}
    try:
        res = linear_gql({
            "query": """mutation($input: IssueCreateInput!) {
                issueCreate(input: $input) { success issue { identifier url } } }""",
            "variables": {"input": {"teamId": meta["team_id"], "title": title[:200],
                                    "assigneeId": meta["viewer_id"]}}})["issueCreate"]
    except Exception as e:
        return {"ok": False, "output": f"linear: {e}"}
    if not res.get("success"):
        return {"ok": False, "output": "issue create failed"}
    _ttl_cache.pop("linear", None)
    return {"ok": True, "issue": res["issue"]}


def save_linear_key(key):
    os.makedirs(os.path.dirname(LINEAR_ENV), mode=0o700, exist_ok=True)
    fd = os.open(LINEAR_ENV, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(f"LINEAR_API_KEY={key.strip()}\n")
    _ttl_cache.pop("linear", None)


HOME = os.path.expanduser("~")
# $/MTok (input, output) by model-id substring, first match wins. Cache read
# bills 0.1x input; cache write 1.25x (5m TTL) / 2x (1h TTL).
CLAUDE_PRICES = [
    ("fable", (10, 50)), ("mythos", (10, 50)), ("opus-4-1", (15, 75)),
    ("opus", (5, 25)), ("sonnet", (3, 15)), ("haiku", (1, 5)),
]


def _claude_cost(model, u):
    p = next((v for k, v in CLAUDE_PRICES if k in (model or "")), (5, 25))
    cc = u.get("cache_creation") or {}
    w5, w1 = cc.get("ephemeral_5m_input_tokens"), cc.get("ephemeral_1h_input_tokens")
    if w5 is None and w1 is None:
        w5, w1 = u.get("cache_creation_input_tokens", 0), 0
    return (u.get("input_tokens", 0) * p[0]
            + u.get("output_tokens", 0) * p[1]
            + u.get("cache_read_input_tokens", 0) * p[0] * 0.1
            + (w5 or 0) * p[0] * 1.25 + (w1 or 0) * p[0] * 2) / 1e6


def collect_agent_usage():
    """Local-first usage stats: Claude Code transcripts (~/.claude/projects,
    per-message usage with dedupe) and Codex rollouts (~/.codex/sessions,
    cumulative totals per session + the latest rate-limit windows)."""
    def fetch():
        import datetime
        now = time.time()
        cutoffs = {"today": now - 86400, "week": now - 7 * 86400}
        claude = {k: {"in": 0, "out": 0, "cache": 0, "cost": 0.0, "msgs": 0}
                  for k in cutoffs}
        seen = set()
        for f in glob.glob(f"{HOME}/.claude/projects/*/*.jsonl"):
            try:
                if os.path.getmtime(f) < cutoffs["week"]:
                    continue
                with open(f) as fh:
                    for line in fh:
                        if '"usage"' not in line:
                            continue
                        try:
                            d = json.loads(line)
                        except ValueError:
                            continue
                        if d.get("type") != "assistant":
                            continue
                        m = d.get("message") or {}
                        u = m.get("usage") or {}
                        if not (u.get("output_tokens") or u.get("input_tokens")):
                            continue
                        key = (m.get("id"), d.get("requestId"))
                        if key in seen:
                            continue
                        seen.add(key)
                        try:
                            ts = datetime.datetime.fromisoformat(
                                d.get("timestamp", "").replace("Z", "+00:00")).timestamp()
                        except ValueError:
                            continue
                        cost = _claude_cost(m.get("model"), u)
                        for k, cut in cutoffs.items():
                            if ts >= cut:
                                b = claude[k]
                                b["in"] += u.get("input_tokens", 0)
                                b["out"] += u.get("output_tokens", 0)
                                b["cache"] += u.get("cache_read_input_tokens", 0)
                                b["cost"] += cost
                                b["msgs"] += 1
            except OSError:
                continue
        codex = {k: {"in": 0, "cached": 0, "out": 0, "sessions": 0} for k in cutoffs}
        limits, newest = None, 0
        for f in glob.glob(f"{HOME}/.codex/sessions/*/*/*/rollout-*.jsonl"):
            try:
                mt = os.path.getmtime(f)
                if mt < cutoffs["week"]:
                    continue
                last_u = last_rl = None
                with open(f) as fh:
                    for line in fh:
                        if '"token_count"' not in line:
                            continue
                        try:
                            d = json.loads(line)
                        except ValueError:
                            continue
                        p = d.get("payload") or {}
                        if p.get("type") != "token_count":
                            continue
                        info = p.get("info") or {}
                        if info.get("total_token_usage"):
                            last_u = info["total_token_usage"]
                        if p.get("rate_limits"):
                            last_rl = p["rate_limits"]
                if last_u:  # cumulative per session file — count the final total
                    for k, cut in cutoffs.items():
                        if mt >= cut:
                            b = codex[k]
                            b["in"] += last_u.get("input_tokens", 0)
                            b["cached"] += last_u.get("cached_input_tokens", 0)
                            b["out"] += last_u.get("output_tokens", 0)
                            b["sessions"] += 1
                if last_rl and mt > newest:
                    newest, limits = mt, last_rl
            except OSError:
                continue
        slim = None
        if limits:
            slim = {"plan": limits.get("plan_type")}
            for name in ("primary", "secondary"):
                w = limits.get(name) or {}
                slim[name] = {"pct": w.get("used_percent"),
                              "mins": w.get("window_minutes"),
                              "resets_at": w.get("resets_at")}
        return {"claude": claude, "codex": codex, "codex_limits": slim}
    return cached("agent-usage", 300, fetch)


def collect_backups():
    base = f"{GRAVE_ROOT}/backups"
    try:
        entries = sorted(d for d in os.listdir(base) if os.path.isdir(f"{base}/{d}"))
    except OSError:
        entries = []
    return {"count": len(entries), "latest": entries[-1] if entries else None}


def boot_mode():
    rc, _, _ = sh(["systemctl", "is-enabled", "--quiet", "t3code"])
    return "developer" if rc == 0 else "gaming"


def gamewatch_state():
    """Game-mode auto-throttle: installed? on? watcher running? (Steam Machine)."""
    installed = sh(["systemctl", "cat", "gravedecay-gamewatch.service"])[0] == 0
    on = os.path.exists(os.path.join(GRAVE_ROOT, "config", "gamewatch.on"))
    running = sh(["systemctl", "is-active", "--quiet", "gravedecay-gamewatch"])[0] == 0
    return {"installed": installed, "on": on, "running": running}


def state(headers):
    t3 = unit_state("t3code")
    mode = "developer" if t3["active"] == "active" else "gaming"
    tmux = collect_tmux()
    try:  # sessions parked in the kernel freezer (grave gaming, pause tier)
        with open("/sys/fs/cgroup/grave-torpor/cgroup.freeze") as f:
            frozen = f.read().strip() == "1"
    except OSError:
        frozen = False
    if mode == "gaming":
        # Minimal footprint while gaming: no remote API calls, no git walks —
        # just vitals. The client also slows its poll to 30 s.
        return {
            "host": HOST, "now": time.strftime("%H:%M:%S"),
            "viewer": headers.get("Tailscale-User-Login", "local"),
            "mode": mode, "apps": list(APPS), "settings": load_settings(),
            "boot_mode": boot_mode(), "gamewatch": gamewatch_state(),
            "tmux": tmux, "torpor": len(tmux) if frozen else 0,
            "system": collect_system(),
            "github": {"login": None, "prs": [], "error": "paused in game mode"},
            "linear": {"configured": False, "issues": [], "error": None},
            "ci": {"rows": []}, "usage": None, "services": [], "repos": [],
            "docker": {"error": "docker stopped (gaming)", "containers": []},
            "journal": [], "backups": {"count": 0, "latest": None},
        }
    viewer = headers.get("Tailscale-User-Login")
    if viewer is not None and viewer not in ALLOWED_USERS:
        # Read-only tailnet viewer (not in ALLOWED_USERS): serve operational
        # vitals but withhold owner-private data — open PR titles, the Linear
        # backlog, agent spend, repo names/commit subjects, CI detail, and journal
        # error lines are not "status". The file manager and actions are already
        # gated by _forbidden; this closes the same gap on /api/state (and / boot).
        return {
            "host": HOST, "now": time.strftime("%H:%M:%S"), "viewer": viewer,
            "mode": mode, "boot_mode": boot_mode(), "gamewatch": gamewatch_state(),
            "apps": list(APPS), "settings": load_settings(),
            "github": {"login": None, "prs": [], "error": "restricted"},
            "linear": {"configured": False, "issues": [], "error": None},
            "ci": {"rows": []}, "usage": None,
            "services": collect_services(), "docker": collect_docker(),
            "tmux": tmux, "torpor": len(tmux) if frozen else 0,
            "repos": [], "journal": [], "system": collect_system(),
            "backups": collect_backups(),
        }
    gh = collect_github()
    apps = list(APPS)
    if gh["login"]:
        apps.append({"name": "🐙 GitHub",
                     "url": f"https://github.com/{gh['login']}?tab=repositories"})
    return {
        "host": HOST,
        "now": time.strftime("%H:%M:%S"),
        "viewer": headers.get("Tailscale-User-Login", "local"),
        "mode": mode,
        "boot_mode": boot_mode(),
        "gamewatch": gamewatch_state(),
        "apps": apps,
        "github": gh,
        "ci": collect_ci(),
        "linear": collect_linear(),
        "usage": collect_agent_usage(),
        "settings": load_settings(),
        "services": collect_services(),
        "docker": collect_docker(),
        "tmux": tmux,
        "torpor": 0,
        "repos": collect_repos(),
        "journal": collect_journal(),
        "system": collect_system(),
        "backups": collect_backups(),
    }


# ---------- file manager ----------

def _safe_path(rel):
    """Resolve a client-supplied relative path inside the FILES_ROOT jail.
    Returns an absolute realpath, or None if it escapes the root (via `..` or a
    symlink) or lands in a denied subtree (the appliance's secret store).
    realpath resolves symlinks, so a link pointing outside the tree is refused
    — that is deliberate: it also means the repos/gravedecay recovery symlink
    (→ ~/dev/gravedecay) is invisible here; edit that repo via git/T3."""
    rel = (rel or "").replace("\\", "/").lstrip("/")
    full = os.path.realpath(os.path.join(FILES_ROOT, rel))
    if full != FILES_ROOT and not full.startswith(FILES_ROOT + os.sep):
        return None
    for deny in FILES_DENY:
        if full == deny or full.startswith(deny + os.sep):
            return None
    return full


def _clean_name(name):
    """A single path component, safe to join onto a directory. No separators,
    no traversal, no NUL; capped at 255 bytes like most filesystems."""
    name = os.path.basename((name or "").strip())
    if name in ("", ".", "..") or "/" in name or "\x00" in name:
        return ""
    return name[:255]


def fs_op(data):
    """Mutating file ops (mkdir / rename / delete), each re-jailed via
    _safe_path so a crafted payload can't reach outside FILES_ROOT."""
    op = str(data.get("op", ""))
    rel = str(data.get("path", ""))
    full = _safe_path(rel)
    if full is None:
        return {"ok": False, "output": "path not allowed"}
    try:
        if op == "mkdir":
            if not os.path.isdir(full):
                return {"ok": False, "output": "no such directory"}
            name = _clean_name(data.get("name", ""))
            if not name:
                return {"ok": False, "output": "bad folder name"}
            target = _safe_path(os.path.join(rel, name))
            if target is None:
                return {"ok": False, "output": "path not allowed"}
            os.mkdir(target)
            return {"ok": True, "output": f"created {name}/"}
        if op == "delete":
            if full == FILES_ROOT:
                return {"ok": False, "output": "refusing to delete the root"}
            if os.path.isdir(full) and not os.path.islink(full):
                shutil.rmtree(full)
            else:
                os.remove(full)
            return {"ok": True, "output": "deleted"}
        if op == "rename":
            if full == FILES_ROOT:
                return {"ok": False, "output": "cannot rename the root"}
            name = _clean_name(data.get("name", ""))
            if not name:
                return {"ok": False, "output": "bad name"}
            target = _safe_path(os.path.join(os.path.dirname(rel), name))
            if target is None:
                return {"ok": False, "output": "path not allowed"}
            os.rename(full, target)
            return {"ok": True, "output": f"renamed to {name}"}
    except OSError as e:
        return {"ok": False, "output": str(e)}
    return {"ok": False, "output": "unknown op"}


class Handler(BaseHTTPRequestHandler):
    server_version = "gravedecay/1"
    # HTTP/1.1: _send always sets Content-Length (keep-alive works), and the
    # SSE stream omits it so the handler auto-closes the connection at end.
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # journald gets enough from systemd
        pass

    def _send(self, code, body, ctype="application/json", cache="no-store", headers=None):
        data = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", cache)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(data)

    def _forbidden(self):
        """True (and a 403 already sent) if the tailnet viewer isn't allowed.
        Localhost has no header and is always trusted. Used to gate the file
        manager's GET reads too — listing the filesystem is sensitive, unlike
        the read-only status GETs which any tailnet viewer may see."""
        viewer = self.headers.get("Tailscale-User-Login")
        if viewer is not None and viewer not in ALLOWED_USERS:
            self._send(403, json.dumps({
                "ok": False,
                "output": f"forbidden for {viewer} — add to GRAVEDECAY_ALLOWED_USERS"}))
            return True
        return False

    def _cross_site(self):
        """True (and a 403 already sent) if this state-changing request looks
        cross-site. Auth here is ambient — tailscale serve stamps the requesting
        node's login on EVERY browser request, including one forged by a
        malicious page — so without this a cross-site <img>/form/fetch to a GET
        action or a text/plain POST runs with the victim's identity (CSRF).

        Modern browsers stamp Sec-Fetch-Site and a cross-origin page cannot forge
        it; note another *.ts.net box is 'same-site', so only 'same-origin' (the
        dashboard's own fetch) and 'none' (a user typing the URL) are accepted.
        Requests without the header are non-browser clients (curl, local tooling)
        — allowed, but an Origin whose host mismatches is refused as an
        older-browser fallback."""
        site = self.headers.get("Sec-Fetch-Site")
        if site is not None:
            ok = site in ("same-origin", "none")
        else:
            origin = self.headers.get("Origin")
            ok = (not origin) or urllib.parse.urlparse(origin).netloc == self.headers.get("Host", "")
        if not ok:
            self._send(403, json.dumps({"ok": False, "output": "cross-site request refused"}))
        return not ok

    def _query(self, key, default=""):
        qs = self.path.split("?", 1)[1] if "?" in self.path else ""
        return urllib.parse.parse_qs(qs).get(key, [default])[0]

    def _files_list(self):
        full = _safe_path(self._query("path"))
        if full is None or not os.path.isdir(full):
            self._send(404, json.dumps({"ok": False, "output": "no such directory"}))
            return
        entries = []
        try:
            names = os.listdir(full)
        except OSError as e:
            self._send(500, json.dumps({"ok": False, "output": str(e)}))
            return
        for name in names:
            fp = os.path.join(full, name)
            # hide denied subtrees and symlinks that escape the jail
            if _safe_path(os.path.relpath(fp, FILES_ROOT)) is None:
                continue
            try:
                st = os.stat(fp)
            except OSError:
                continue
            isdir = os.path.isdir(fp)
            entries.append({"name": name, "type": "dir" if isdir else "file",
                            "size": 0 if isdir else st.st_size,
                            "mtime": int(st.st_mtime), "link": os.path.islink(fp)})
        entries.sort(key=lambda e: (e["type"] != "dir", e["name"].lower()))
        rel = os.path.relpath(full, FILES_ROOT)
        self._send(200, json.dumps({"ok": True, "path": "" if rel == "." else rel,
                                    "root": os.path.basename(FILES_ROOT) or "/",
                                    "entries": entries}))

    def _files_download(self):
        full = _safe_path(self._query("path"))
        if full is None or not os.path.isfile(full):
            self._send(404, json.dumps({"ok": False, "output": "no such file"}))
            return
        try:
            size = os.path.getsize(full)
            f = open(full, "rb")
        except OSError as e:
            self._send(500, json.dumps({"ok": False, "output": str(e)}))
            return
        # ASCII-safe filename for the header; non-ascii names still download,
        # just with the fallback label (the browser's own Save dialog wins).
        safe = os.path.basename(full).encode("ascii", "ignore").decode() or "download"
        safe = safe.replace('"', "")
        with f:
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(size))
            self.send_header("Content-Disposition", f'attachment; filename="{safe}"')
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            shutil.copyfileobj(f, self.wfile)

    def _files_upload(self):
        """Single file per request: bytes are the raw body, target dir + name
        ride in the query string. Avoids multipart parsing (cgi is gone in
        3.13+) and streams straight to disk instead of buffering in RAM."""
        rel = self._query("path")
        d = _safe_path(rel)
        if d is None or not os.path.isdir(d):
            self._send(404, json.dumps({"ok": False, "output": "no such directory"}))
            return
        name = _clean_name(self._query("name"))
        if not name:
            self._send(400, json.dumps({"ok": False, "output": "bad filename"}))
            return
        dest = _safe_path(os.path.join(rel, name))
        if dest is None:
            self._send(400, json.dumps({"ok": False, "output": "path not allowed"}))
            return
        length = int(self.headers.get("Content-Length", 0))
        if length > MAX_UPLOAD:
            self._send(413, json.dumps({"ok": False, "output": "file too large"}))
            return
        tmp = dest + ".part"
        try:
            remaining = length
            with open(tmp, "wb") as out:
                while remaining > 0:
                    chunk = self.rfile.read(min(remaining, 1 << 20))
                    if not chunk:
                        break
                    out.write(chunk)
                    remaining -= len(chunk)
            os.replace(tmp, dest)
        except OSError as e:
            try:
                os.remove(tmp)
            except OSError:
                pass
            self._send(500, json.dumps({"ok": False, "output": str(e)}))
            return
        self._send(200, json.dumps({"ok": True, "output": f"uploaded {name}"}))

    def _route(self):
        """Path with the optional BASE mount prefix stripped; None if a
        redirect was already sent (relative URLs need the trailing slash)."""
        p = self.path.split("?", 1)[0]
        if BASE and p == BASE:
            self.send_response(301)
            self.send_header("Location", BASE + "/")
            self.end_headers()
            return None
        if BASE and p.startswith(BASE + "/"):
            p = p[len(BASE):]
        return p

    def _stream_action(self):
        """SSE boot console: runs a grave action and streams its output live
        (data: <json line> events, then event: done with the exit code)."""
        viewer = self.headers.get("Tailscale-User-Login")
        if viewer is not None and viewer not in ALLOWED_USERS:
            self._send(403, json.dumps({"ok": False, "output": f"forbidden for {viewer}"}))
            return
        if self._cross_site():   # actions run on a GET, so <img src=…?action=reboot> is CSRF
            return
        qs = self.path.split("?", 1)[1] if "?" in self.path else ""
        action = dict(kv.split("=", 1) for kv in qs.split("&") if "=" in kv).get("action", "")
        cmd = ACTIONS.get(action)
        if not cmd:
            self._send(400, json.dumps({"ok": False, "output": "unknown action"}))
            return
        if action == "t3-pair":
            host = self.headers.get("Host", "")
            if re.fullmatch(r"[A-Za-z0-9.\-:\[\]]+", host or ""):
                cmd = cmd + ["--base-url", f"https://{host}"]
        import sys
        if not ACTION_LOCK.acquire(blocking=False):
            self._send(409, json.dumps({"ok": False,
                                        "output": "another action is already running — wait for it"}))
            return
        print(f"stream: start action={action} viewer={viewer} proto={self.request_version}",
              file=sys.stderr, flush=True)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        sent = 0
        proc = None
        try:
            # Popen MUST be inside the try: a spawn failure (e.g. binary not on
            # the service PATH) would otherwise skip the finally and leak
            # ACTION_LOCK, wedging every later action behind a 409.
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                    text=True, bufsize=1)
            for line in proc.stdout:
                payload = json.dumps(ANSI.sub("", line.rstrip("\n")))
                self.wfile.write(f"data: {payload}\n\n".encode())
                self.wfile.flush()
                sent += 1
            rc = proc.wait(timeout=180)
            self.wfile.write(f"event: done\ndata: {rc}\n\n".encode())
            self.wfile.flush()
            print(f"stream: done action={action} rc={rc} lines={sent}",
                  file=sys.stderr, flush=True)
        except (BrokenPipeError, ConnectionResetError) as e:
            # Client went away mid-stream. NEVER kill the action — a
            # half-finished mode switch is worse than a lost console — and
            # keep draining stdout so the action isn't SIGPIPE'd either.
            print(f"stream: client gone action={action} after {sent} lines: {e}",
                  file=sys.stderr, flush=True)
            for _ in proc.stdout:
                pass
            proc.wait(timeout=180)
        except subprocess.TimeoutExpired:
            print(f"stream: TIMEOUT action={action}", file=sys.stderr, flush=True)
            proc.kill()
        except OSError as e:
            # Command never launched (missing binary, exec error). Report it on
            # the stream instead of 500'ing; the lock still releases below.
            print(f"stream: spawn failed action={action}: {e}", file=sys.stderr, flush=True)
            try:
                self.wfile.write(f"data: {json.dumps('error: ' + str(e))}\n\n".encode())
                self.wfile.write(b"event: done\ndata: 127\n\n")
                self.wfile.flush()
            except OSError:
                pass
        finally:
            # Close the connection so the client sees EOF. Without this,
            # HTTP/1.1 keep-alive leaves the socket open after the stream —
            # iOS Safari buffers small streamed bodies until EOF, so the
            # console showed one line and then "hung" forever on iPhone.
            self.close_connection = True
            ACTION_LOCK.release()

    def do_GET(self):
        p = self._route()
        if p is None:
            return
        if p == "/api/action-stream":
            self._stream_action()
            return
        if p == "/healthz":
            self._send(200, json.dumps({"ok": True, "build": BUILD_ID}))
        elif p == "/api/state":
            self._send(200, json.dumps(state(self.headers)))
        elif p == "/api/admin/releases":
            if self._forbidden():
                return
            rc, out, err = sh([GRAVE, "releases", "--json"], timeout=30)
            if rc:
                self._send(502, json.dumps({"ok": False, "output": ANSI.sub("", out + err)}))
            else:
                self._send(200, out)
        elif p == "/api/files":
            if self._forbidden():
                return
            self._files_list()
        elif p == "/api/download":
            if self._forbidden():
                return
            self._files_download()
        elif p == "/":
            boot = json.dumps(state(self.headers)).replace("</", "<\\/")
            self._send(200, PAGE.replace("/*BOOT*/null", boot), "text/html; charset=utf-8")
        elif p == "/manifest.webmanifest":
            self._send(200, MANIFEST, "application/manifest+json", "no-cache")
        elif p == "/sw.js":
            self._send(200, static_asset("sw.js", SERVICE_WORKER),
                       "text/javascript; charset=utf-8", "no-cache",
                       {"Service-Worker-Allowed": "/"})
        elif p == "/offline.html":
            self._send(200, static_asset("offline.html", OFFLINE_PAGE),
                       "text/html; charset=utf-8", "public, max-age=86400")
        elif p in ("/apple-touch-icon.png", "/icon-180.png"):
            self._send(200, icon_png(180), "image/png", "public, max-age=86400")
        elif p == "/icon-192.png":
            self._send(200, icon_png(192), "image/png", "public, max-age=86400")
        elif p == "/icon-512.png":
            self._send(200, icon_png(512), "image/png", "public, max-age=86400")
        else:
            self._send(404, '{"error":"not found"}')

    def do_POST(self):
        p = self._route()
        if p is None:
            return
        viewer = self.headers.get("Tailscale-User-Login")
        if viewer is not None and viewer not in ALLOWED_USERS:
            self._send(403, json.dumps({
                "ok": False,
                "output": f"forbidden for {viewer} — add to GRAVEDECAY_ALLOWED_USERS"}))
            return
        if self._cross_site():
            return
        # Upload is a raw-body PUT-style POST — handle it BEFORE the JSON parse
        # below would try to json.loads() a multi-gigabyte file body.
        if p == "/api/upload":
            self._files_upload()
            return
        try:
            length = int(self.headers.get("Content-Length", 0))
            data = json.loads(self.rfile.read(length)) if length else {}
        except ValueError:
            self._send(400, json.dumps({"ok": False, "output": "bad payload"}))
            return
        if p == "/api/fs":
            self._send(200, json.dumps(fs_op(data)))
            return
        if p == "/api/settings":
            try:
                key = data.pop("linear_key", "")
                if isinstance(key, str) and key.strip():
                    save_linear_key(key)
                merged = save_settings(data)
            except (ValueError, TypeError, OSError):
                self._send(400, json.dumps({"ok": False, "output": "bad settings payload"}))
                return
            self._send(200, json.dumps({"ok": True, "settings": merged,
                                        "linear_configured": bool(linear_key())}))
        elif p == "/api/linear-issue":
            title = str(data.get("title", "")).strip()
            if not title:
                self._send(400, json.dumps({"ok": False, "output": "title required"}))
                return
            self._send(200, json.dumps(linear_create(title)))
        elif p == "/api/session-kill":
            name = str(data.get("name", ""))
            if not re.fullmatch(r"[A-Za-z0-9_-]{1,50}", name):
                self._send(400, json.dumps({"ok": False, "output": "bad session name"}))
                return
            rc, out, err = sh(["tmux", "-L", TMUX_SOCKET, "kill-session", "-t", name])
            self._send(200, json.dumps({"ok": rc == 0, "output": out + err}))
        elif p == "/api/admin/upgrade":
            tag = str(data.get("tag", ""))
            if not re.fullmatch(r"v[0-9]+\.[0-9]+\.[0-9]+", tag):
                self._send(400, json.dumps({"ok": False, "output": "invalid release tag"}))
                return
            unit = f"gravedecay-upgrade@{tag}.service"
            rc, out, err = sh(["sudo", "-n", "systemctl", "--no-block", "start", unit])
            self._send(200 if rc == 0 else 500, json.dumps({
                "ok": rc == 0,
                "output": "upgrade queued; the dashboard will reconnect" if rc == 0
                else ANSI.sub("", out + err),
            }))
        elif p == "/api/action":
            try:
                cmd = ACTIONS[data["action"]]
            except KeyError:
                self._send(400, json.dumps({"ok": False, "output": "unknown action"}))
                return
            if not ACTION_LOCK.acquire(blocking=False):
                self._send(409, json.dumps({"ok": False,
                                            "output": "another action is already running"}))
                return
            try:
                rc, out, err = sh(cmd, timeout=120)
            finally:
                ACTION_LOCK.release()
            self._send(200, json.dumps({"ok": rc == 0, "output": ANSI.sub("", out + err)}))
        else:
            self._send(404, '{"error":"not found"}')


PAGE = r"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<script>
// CRITICAL: if opened at the mount point WITHOUT a trailing slash
// (https://box/grave), every relative URL on this page — manifest, icons,
// api/state, api/action-stream — resolves against the ORIGIN ROOT and lands
// on T3 instead of this dashboard. The backend cannot 301 it because
// tailscale serve strips the mount prefix before proxying. Fix the base
// before the parser touches any href/src below.
if(!location.pathname.endsWith('/'))history.replaceState(null,'',location.pathname+'/');
</script>
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="theme-color" content="#070907">
<meta name="mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="apple-mobile-web-app-title" content="gravedecay">
<link rel="manifest" href="manifest.webmanifest">
<link rel="apple-touch-icon" href="apple-touch-icon.png">
<link rel="icon" type="image/png" href="icon-192.png">
<title>gravedecay · @HOST@</title>
<style>
/* grave-term — native terminal skin. Phosphor green on near-black, square
   corners, panel titles on the border, inverted hovers, scanlines + faint CRT
   glow. Themeable via the :root block (swap for gruvbox/amber/etc). */
:root{
  --page:#070907; --surface:#0a0d0a; --inset:#050705;
  --ink:#d6ffd0; --ink-2:#a8e6a3; --muted:#557a55; --hairline:#1c2b1c;
  --ring:#2e4a2e; --title:#ffb000;
  --accent:#ffb000; --accent-soft:#7dd87d;
  --good:#39d353; --warn:#ffb000; --crit:#ff5f56;
  --track-blue:#12240f; --track-warn:#332600; --track-crit:#331111;
  --glow:0 0 7px rgba(120,255,120,.28);
}
*{box-sizing:border-box;margin:0;-webkit-tap-highlight-color:transparent}
html{-webkit-text-size-adjust:100%}
body{background:var(--page);color:var(--ink-2);
  font:13.5px/1.5 ui-monospace,'JetBrains Mono','Fira Code',Menlo,Consolas,monospace;
  padding:0 max(14px,env(safe-area-inset-left)) calc(24px + env(safe-area-inset-bottom))
    max(14px,env(safe-area-inset-right));max-width:1120px;min-width:0;margin:0 auto}
/* CRT: fixed scanlines + vignette, zero layout cost */
body::before{content:'';position:fixed;inset:0;pointer-events:none;z-index:99;
  background:repeating-linear-gradient(0deg,transparent 0 2px,rgba(0,0,0,.13) 2px 3px)}
body::after{content:'';position:fixed;inset:0;pointer-events:none;z-index:98;
  background:radial-gradient(ellipse at 50% 40%,transparent 60%,rgba(0,0,0,.32))}
::selection{background:var(--accent);color:#000}
a{color:var(--accent);text-decoration:none}
a:hover{background:var(--accent);color:#000}
h1{font-size:17px;font-weight:700;color:var(--ink);text-shadow:var(--glow)}
#toplogo{width:36px;height:36px;display:block}
.topbar{position:sticky;top:0;z-index:101;display:flex;flex-wrap:nowrap;gap:10px;align-items:center;
  background:var(--page);margin:0 -14px 16px;
  padding:calc(16px + env(safe-area-inset-top)) max(14px,env(safe-area-inset-right)) 14px
    max(14px,env(safe-area-inset-left));
  border-bottom:1px solid var(--ring)}
.topbar h1{white-space:nowrap}
/* iOS PWA: an opaque strip over the status-bar/dynamic-island area — content
   must never be readable up there while scrolling. Sits above the scanline
   overlays (z 98/99); zero height on devices without an inset. */
/* z 97: below the scanline/vignette overlays (98/99) so the safe-area strip
   gets the same texture as the rest of the page — no faint seam — while
   still covering scrolled content (z auto). */
#topcover{position:fixed;top:0;left:0;right:0;height:env(safe-area-inset-top);
  background:var(--page);z-index:97}
/* meta takes the slack and truncates — the controls can never wrap off-row */
.topbar .meta{color:var(--muted);font-size:12px;flex:1;min-width:0;
  white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
#connection{display:none;margin:-8px 0 14px;padding:8px 10px;border:1px solid var(--warn);
  background:var(--track-warn);color:var(--warn);font-size:12px}
#connection.show{display:flex;align-items:center;justify-content:space-between;gap:10px}
#connection button{min-height:32px;padding:3px 9px;font-size:12px;flex:none}
.badge{display:inline-flex;align-items:center;gap:6px;padding:3px 10px;
  border:1px solid var(--ring);font-size:12px;font-weight:700;color:var(--ink)}
#mode{flex-shrink:0} .topbar .gear{flex-shrink:0}
#mode:hover{border-color:var(--accent);color:var(--accent)}
/* launcher tiles */
.apps{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:8px;margin-bottom:14px}
.app{display:flex;align-items:center;justify-content:center;gap:8px;min-height:52px;
  background:var(--surface);border:1px solid var(--ring);
  min-width:0;padding:7px;text-align:center;overflow-wrap:anywhere;
  font-weight:700;font-size:14px;color:var(--ink)}
.app::before{content:'▸ ';color:var(--muted)}
.app:hover{background:var(--ink-2);color:#000;text-shadow:none}
.app:hover::before{color:#000}
.app:active{transform:scale(.97)}
/* tabs as bracket toggles */
.tabs{display:flex;gap:8px;margin-bottom:16px}
.tab{flex:1;min-height:38px;padding:6px 12px;color:var(--muted);font-weight:700;
  letter-spacing:.08em;text-transform:uppercase;font-size:12px}
.tab::before{content:'[ '} .tab::after{content:' ]'}
.tab.active{background:var(--ink-2);color:#000;border-color:var(--ink-2)}
/* buttons */
button{background:transparent;color:var(--ink);border:1px solid var(--ring);
  border-radius:0;padding:10px 16px;min-height:44px;
  font:700 13px ui-monospace,Menlo,monospace;cursor:pointer;touch-action:manipulation}
button:hover{background:var(--ink-2);color:#000;border-color:var(--ink-2)}
button:active{transform:scale(.97)}
button:disabled{opacity:.35;cursor:default;transform:none;background:transparent;color:var(--ink)}
button.busy{opacity:.6;cursor:wait}
.gear{min-height:0;padding:3px 9px;font-size:13px}
@media(pointer:coarse){td{padding-top:9px;padding-bottom:9px}}
/* panels: title sits ON the border, like a TUI frame */
#panels{display:grid;grid-template-columns:1fr 1fr;gap:18px 12px;margin-bottom:12px}
/* rows stretch: side-by-side widgets always share the taller one's height */
@media(max-width:760px){#panels{grid-template-columns:1fr}}
.w-full{grid-column:1/-1}
.panel{position:relative;background:var(--surface);border:1px solid var(--ring);
  border-radius:0;padding:16px 12px 10px;min-width:0;container:dashboard-panel / inline-size}
.panel h2{position:absolute;top:-8px;left:10px;background:var(--page);padding:0 7px;
  max-width:calc(100% - 20px);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;
  font-size:11px;font-weight:700;color:var(--title);letter-spacing:.08em;
  text-transform:uppercase}
/* stat tiles */
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(148px,100%),1fr));gap:10px;
  background:transparent;border:none;padding:0}
.tile{background:var(--surface);border:1px solid var(--ring);padding:10px 12px;min-width:0}
.tile .label{font-size:11px;color:var(--muted);margin-bottom:4px;
  text-transform:uppercase;letter-spacing:.07em}
.tile .value{font-size:23px;font-weight:700;color:var(--ink);text-shadow:var(--glow)}
.tile .sub{font-size:11px;color:var(--muted);margin-top:2px}
/* meters: segmented bar */
.meter{height:9px;margin-top:8px;background:var(--track-blue);border:1px solid var(--hairline);overflow:hidden}
.meter i{display:block;height:100%;background:repeating-linear-gradient(90deg,
  var(--good) 0 5px,transparent 5px 7px)}
.meter.warn{background:var(--track-warn)}
.meter.warn i{background:repeating-linear-gradient(90deg,var(--warn) 0 5px,transparent 5px 7px)}
.meter.crit{background:var(--track-crit)}
.meter.crit i{background:repeating-linear-gradient(90deg,var(--crit) 0 5px,transparent 5px 7px)}
/* tables */
table{width:100%;max-width:100%;border-collapse:collapse;font-size:13px}
td{min-width:0;padding:4px 8px 4px 0;border-top:1px dashed var(--hairline);
  vertical-align:top;overflow-wrap:anywhere;word-break:break-word}
tr:first-child td{border-top:none}
td.num{text-align:right;font-variant-numeric:tabular-nums;color:var(--ink-2)}
td.dim{color:var(--muted)}
/* keys/ids in the first column (WEC-24, repo #3, …) must never wrap */
#linear td:first-child,#prs td:first-child,#reviews td:first-child,
#ci td:first-child,#usage td:first-child{white-space:nowrap;padding-right:14px}
/* status squares (■), not dots */
.dot{display:inline-block;width:8px;height:8px;margin-right:7px;vertical-align:-1px}
.st-good{background:var(--good);box-shadow:0 0 5px var(--good)}
.st-warn{background:var(--warn);box-shadow:0 0 5px var(--warn)}
.st-crit{background:var(--crit);box-shadow:0 0 5px var(--crit)}
pre{background:var(--inset);border:1px solid var(--hairline);padding:10px;
  font:12px/1.55 ui-monospace,monospace;overflow-x:auto;white-space:pre-wrap;color:var(--ink-2)}
.spark{display:block;margin-top:6px}
.full{margin-bottom:10px}
.kill{color:var(--crit);cursor:pointer;padding:0 6px}
.kill:hover{background:var(--crit);color:#000}
/* game mode banner */
#game-banner{display:none;border:1px solid var(--crit);padding:18px;margin-bottom:14px;
  text-align:center;background:#120a0a;animation:pulse 2.4s ease-in-out infinite}
#game-banner h2{font-size:20px;color:var(--crit);letter-spacing:.2em;margin-bottom:6px;
  text-shadow:0 0 9px rgba(255,95,86,.5)}
#game-banner .dim2{color:var(--muted);font-size:12px;margin-bottom:12px}
@keyframes pulse{0%,100%{box-shadow:0 0 0 0 rgba(255,95,86,.3)}
  50%{box-shadow:0 0 22px 4px rgba(255,95,86,.18)}}
body.gaming .tabs{display:none}
body.gaming #panels>*{display:none!important}
body.gaming #panels>[data-panel="stats"]{display:grid!important}
/* overlays: console + dialogs */
.overlay{position:fixed;inset:0;z-index:110;background:rgba(3,5,3,.9);
  height:100vh;height:100dvh;
  backdrop-filter:blur(3px);-webkit-backdrop-filter:blur(3px);padding:14px;
  padding-top:calc(14px + env(safe-area-inset-top));overflow-y:auto;
  -webkit-overflow-scrolling:touch;overscroll-behavior:contain}
.sec-toggle{cursor:pointer;user-select:none;-webkit-user-select:none}
#foot{margin:34px 0 6px;text-align:center;color:var(--muted);font-size:11px;
  letter-spacing:.06em}
#foot #epitaph{font-style:italic}
body.gaming #foot{display:none}
.dlg{max-width:860px;margin:5vh auto;background:var(--inset);border:1px solid var(--ring);
  padding:16px;position:relative}
#console-title{font-size:13px;font-weight:700;color:var(--title);margin-bottom:10px;
  letter-spacing:.06em}
#console-out{max-height:62vh;overflow-y:auto;background:transparent;border:none;padding:0;
  font-size:13px;line-height:1.6;color:var(--ink-2)}
#console-out .hl{color:var(--title)} #console-out .err{color:var(--crit)}
#ccur{color:var(--ink-2);animation:blink 1s steps(1) infinite}
@keyframes blink{50%{opacity:0}}
#gc-box{border-color:#4a2222}
#gc-box h2{color:var(--ink);font-size:15px;margin-bottom:8px}
#gc-box p{margin:6px 0;font-size:13px}
.dim2{color:var(--muted)}
/* settings */
#gc-box2{border-color:#4a2222}
.setrow{display:flex;gap:8px;align-items:center;margin:7px 0;flex-wrap:wrap;font-size:13px}
.setrow input,.setrow select{background:var(--inset);border:1px solid var(--hairline);
  color:var(--ink);border-radius:0;padding:7px 9px;min-width:0;max-width:100%;
  font:13px ui-monospace,Menlo,monospace}
.setrow input:focus,.setrow select:focus{outline:1px solid var(--accent)}
/* text-only buttons must not render smaller than their emoji-bearing
   siblings — normalize mini height; corner ✕ gets a fixed square */
.mini{min-height:32px;padding:4px 10px;font-size:13px}
.xbtn{width:34px;height:34px;min-height:34px;padding:0;font-size:16px;
  line-height:1;display:inline-flex;align-items:center;justify-content:center}
.mini.activebtn{background:var(--ink-2);color:#000;border-color:var(--ink-2)}
.abtn{display:inline-flex;align-items:center;background:transparent;color:var(--ink);
  border:1px solid var(--ring);cursor:pointer}
.abtn:hover{background:var(--ink-2);color:#000}
.setlabel{flex:1 1 130px;color:var(--ink-2)}
.sethead{font-size:11px;font-weight:700;color:var(--title);text-transform:uppercase;
  letter-spacing:.08em;margin:14px 0 2px}
/* thin dark scrollbars */
::-webkit-scrollbar{width:8px;height:8px}
::-webkit-scrollbar-thumb{background:var(--ring)}
::-webkit-scrollbar-track{background:transparent}
/* action button row (inside the Actions panel) */
.actions{display:flex;flex-wrap:wrap;gap:8px}
@media(max-width:640px){.actions{display:grid;grid-template-columns:1fr 1fr}}
/* file manager */
#files-box{max-width:720px}
#files-crumb{margin:2px 0 8px;font-size:12px;word-break:break-all}
#files-crumb a{color:var(--accent-soft);cursor:pointer}
#files-drop{border:1px solid var(--hairline);max-height:56vh;overflow-y:auto;
  -webkit-overflow-scrolling:touch}
#files-drop.drag{outline:2px dashed var(--accent);outline-offset:-4px;background:var(--surface)}
.frow{display:flex;align-items:center;gap:8px;padding:7px 9px;
  border-bottom:1px solid var(--hairline)}
.frow:last-child{border-bottom:0}
.frow[data-type="dir"]{cursor:pointer}
.frow:hover{background:var(--surface)}
.frow .fname{flex:1;color:var(--ink);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.frow[data-type="dir"] .fname{color:var(--accent-soft)}
.frow .fmeta{color:var(--muted);font-size:11px;min-width:44px;text-align:right}
.frow .mini{min-height:26px;padding:2px 7px;font-size:12px}
#files-empty{padding:14px;color:var(--muted);text-align:center}
/* app iframe modal — a small app opened over the dashboard (never T3) */
#appframe-box{max-width:1120px;width:96vw;height:88vh;height:88dvh;margin:4vh auto;padding:0;
  display:flex;flex-direction:column;overflow:hidden}
#appframe-bar{display:flex;align-items:center;gap:8px;padding:8px 10px;
  border-bottom:1px solid var(--ring);flex:none}
#appframe-title{flex:1;color:var(--title);font-size:12px;font-weight:700;
  letter-spacing:.06em;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
#appframe-if{flex:1;width:100%;border:0;background:var(--page)}
/* A panel can become narrow in iPad Split View or a two-column desktop
   layout even when the viewport itself is wide. Reflow records based on the
   component's real width rather than guessing from device categories. */
@container dashboard-panel (max-width:500px){
  table,tbody,tr,td{display:block;width:100%}
  tr{padding:7px 0;border-top:1px dashed var(--hairline)}
  tr:first-child{border-top:0}
  td{padding:1px 0;border:0!important;white-space:normal!important}
  td.num{text-align:left}
  td:empty{display:none}
  #tmux tr{display:grid;grid-template-columns:minmax(0,1fr) auto auto;gap:0 8px}
  #tmux td:first-child{grid-column:1/-1}
  #tmux td.num{text-align:right;width:auto}
  #tmux td:last-child{grid-column:3;width:auto}
}
/* Safari fallback: older installed PWAs can ignore container queries even
   though the phone viewport is narrow.  Repeat the record reflow behind a
   universally-supported viewport query so live usage/repo/session values can
   never keep their desktop table width and paint past the panel edge. */
@media(max-width:500px){
  .panel table,.panel tbody,.panel tr,.panel td{display:block;width:100%}
  .panel tr{padding:7px 0;border-top:1px dashed var(--hairline)}
  .panel tr:first-child{border-top:0}
  .panel td{padding:1px 0;border:0!important;white-space:normal!important}
  .panel td.num{text-align:left}
  .panel td:empty{display:none}
  .panel #tmux tr{display:grid;grid-template-columns:minmax(0,1fr) auto auto;gap:0 8px}
  .panel #tmux td:first-child{grid-column:1/-1}
  .panel #tmux td.num{text-align:right;width:auto}
  .panel #tmux td:last-child{grid-column:3;width:auto}
}
.release-picker{margin-top:12px;padding-top:10px;border-top:1px dashed var(--hairline)}
.release-picker label{color:var(--muted)}
.release-picker select{background:var(--inset);color:var(--ink);border:1px solid var(--ring);
  min-height:40px;padding:6px 9px;font:13px ui-monospace,Menlo,monospace}
#grave-release-state{flex:1 1 180px;color:var(--muted)}
/* Compact phones: preserve every action, but remove the last intrinsic-width
   traps and make controls comfortable without allowing horizontal scrolling. */
@media(max-width:520px){
  body{padding-left:max(8px,env(safe-area-inset-left));
    padding-right:max(8px,env(safe-area-inset-right));font-size:13px}
  .topbar{gap:7px;margin-left:-8px;margin-right:-8px;
    padding-left:max(8px,env(safe-area-inset-left));
    padding-right:max(8px,env(safe-area-inset-right))}
  #toplogo{width:32px;height:32px}.topbar h1{font-size:15px}
  .topbar .meta{font-size:11px}.badge{padding:3px 7px}
  .apps{grid-template-columns:repeat(2,minmax(0,1fr))}
  .tabs{gap:6px}.tab{padding:6px 4px;letter-spacing:.04em}
  .panel{padding-left:9px;padding-right:9px}
  .actions{grid-template-columns:1fr}
  .overlay{padding-left:max(8px,env(safe-area-inset-left));
    padding-right:max(8px,env(safe-area-inset-right))}
  .dlg{padding:13px;margin:2vh auto;max-width:100%}
  .setrow>*{max-width:100%}
  .setrow input:not([type="checkbox"]),.setrow select{flex:1 1 100%;width:100%}
  #appframe-box{width:100%;height:92vh;height:92dvh;margin:1vh auto}
  #appframe-open{padding-left:7px;padding-right:7px}
}
@media(max-width:360px){
  #toplogo{display:none}.topbar h1{font-size:14px}
  .topbar .meta{display:none}
  .apps{grid-template-columns:1fr}
}
</style></head><body>
<div id="topcover"></div>
<div class="topbar">
  <img id="toplogo" src="icon-180.png" alt="">
  <h1>gravedecay</h1>
  <span class="meta" id="meta">connecting…</span>
  <span class="badge" id="mode" role="button" title="Tap to switch mode" style="cursor:pointer">…</span>
  <button class="gear" id="gear" title="Settings" aria-label="Settings">⚙️</button>
</div>
<div id="connection" role="status" aria-live="polite"><span id="connection-text"></span>
  <button id="connection-retry">↻ retry</button></div>
<div class="apps" id="apps"></div>
<div id="game-banner">
  <h2>🎮 G A M E &nbsp; M O D E</h2>
  <div class="dim2" id="torpor-line"></div>
  <button id="wake">💻 Wake the dev stack</button>
</div>
<div class="tabs">
  <button class="tab" data-tab="work">🛠️ Work</button>
  <button class="tab" data-tab="system">📟 System</button>
</div>
<div class="overlay" id="settings-panel" style="display:none">
 <div class="dlg">
  <button class="mini xbtn" id="settings-x" title="close (Esc)" aria-label="close"
    style="position:absolute;top:10px;right:10px;z-index:2">✕</button>
  <h2 style="color:var(--ink);font-size:15px;margin-bottom:10px">⚙️ Settings</h2>

  <div class="sethead sec-toggle" data-sec="sec-apps">▸ Launcher tiles — 👁 show · ⚡ skip-perms · ▢ modal · ↗ new tab</div>
  <div id="sec-apps" style="display:none">
    <div id="set-apps"></div>
    <div class="setrow">
      <input id="new-app-name" placeholder="label (e.g. 🎬 Jellyfin)" size="16">
      <input id="new-app-url" placeholder="/path or https://…" size="20">
      <button class="mini" id="add-app">＋ add tile</button>
    </div>
  </div>

  <div class="sethead sec-toggle" data-sec="sec-widgets">▸ Widgets — show &amp; order</div>
  <div id="sec-widgets" style="display:none">
    <div id="set-widgets"></div>
  </div>

  <div class="sethead">Boot mode — what a reboot starts</div>
  <div class="setrow">
    <button class="mini" id="boot-dev">💻 developer</button>
    <button class="mini" id="boot-game">🎮 gaming</button>
    <span class="setlabel dim2" id="boot-state"></span>
  </div>

  <div class="sethead" id="throttle-head" style="display:none">Game-mode auto-throttle<span class="dim2" id="throttle-info" role="button" tabindex="0" title="What is this?" style="cursor:pointer;margin-left:7px;border:1px solid var(--ring);border-radius:50%;padding:0 5px">ⓘ</span></div>
  <div class="setrow" id="throttle-row" style="display:none">
    <button class="mini" id="throttle-on">🎮 on</button>
    <button class="mini" id="throttle-off">⏸ off</button>
    <span class="setlabel dim2" id="throttle-state"></span>
  </div>

  <div class="sethead">Auth &amp; pairing</div>
  <div class="setrow">
    <button class="mini" id="t3-pair-btn">🔑 New T3 pairing token</button>
    <a class="mini abtn" data-auth="auth-claude">🤖 Re-auth Claude</a>
    <a class="mini abtn" data-auth="auth-codex">🧠 Re-auth Codex</a>
    <a class="mini abtn" data-auth="auth-github">🐙 Re-auth GitHub</a>
  </div>

  <div class="sethead">Integrations</div>
  <div class="setrow"><span class="setlabel">Linear API key <span id="linear-state"></span></span>
    <input type="password" id="set-linear" placeholder="lin_api_… (leave empty to keep)" size="24">
  </div>

  <div class="sethead">Refresh</div>
  <div class="setrow"><span class="setlabel">poll interval</span>
    <select id="set-poll">
      <option value="2000">2 s</option><option value="5000">5 s</option>
      <option value="10000">10 s</option><option value="30000">30 s</option>
    </select>
  </div>

  <div class="setrow" style="position:sticky;bottom:0;background:var(--inset);padding-top:8px">
    <button id="save-set">💾 Save</button>
    <button id="close-set">Close</button><span id="set-msg" class="setlabel"></span></div>
 </div>
</div>
<div class="overlay" id="kill-dlg" style="display:none">
 <div class="dlg" id="gc-box2">
  <button class="mini xbtn" id="kill-x" style="position:absolute;top:10px;right:10px;z-index:2">✕</button>
  <h2 style="color:var(--ink);font-size:15px;margin-bottom:8px">🗡️ Kill sessions</h2>
  <p class="dim2" style="font-size:13px;margin-bottom:8px">Pick what dies. Anything running inside dies with it.</p>
  <div id="kill-list"></div>
  <div class="setrow"><button class="mini" id="kill-all">☠️ Kill ALL sessions</button>
    <span class="setlabel dim2" id="kill-msg"></span></div>
 </div>
</div>
<div class="overlay" id="console" style="display:none">
  <div class="dlg">
    <button class="mini xbtn" id="console-x" title="close (Esc)" aria-label="close"
      style="position:absolute;top:10px;right:10px;z-index:2">✕</button>
    <div id="console-title">▚ grave</div>
    <pre id="console-out"></pre><span id="ccur">▮</span>
    <div class="setrow"><button id="console-close" style="display:none">Close</button></div>
  </div>
</div>
<div class="overlay" id="game-confirm" style="display:none">
  <div class="dlg" id="gc-box">
    <h2>🎮 Enter game mode?</h2>
    <p class="dim2">Stops: T3 Code, docker + all container stacks (postgres, redis, browsers).<br>
       Keeps: Tailscale, SSH, this dashboard, the web terminal.</p>
    <p id="gc-sessions"></p>
    <div class="setrow">
      <button id="gc-freeze">🧊 Freeze sessions &amp; game</button>
      <button id="gc-kill">☠️ Kill sessions &amp; game</button>
      <button id="gc-cancel">Cancel</button>
    </div>
  </div>
</div>
<div class="overlay" id="throttle-dlg" style="display:none">
  <div class="dlg">
    <button class="mini xbtn" id="throttle-x" title="close (Esc)" aria-label="close"
      style="position:absolute;top:10px;right:10px;z-index:2">✕</button>
    <h2>🎮 Game-mode auto-throttle</h2>
    <p class="dim2">This box does double duty — your agents work while you're away,
      and it's a console when you want to play. Auto-throttle makes the hand-off
      automatic, so you never have to flip modes yourself.</p>
    <p><b>Launch a game</b> → agent sessions are <b>frozen</b> in place (kept in
      RAM, provably zero CPU) and T3 Code + the database containers stop, handing
      all the RAM and GPU to the game.</p>
    <p><b>Quit the game</b> → everything <b>thaws and resumes</b> right where it
      left off, mid-thought.</p>
    <p class="dim2">Tailscale, SSH, this dashboard and the web terminal always stay
      up — the box is reachable even mid-game. A running game is detected via
      SteamOS's GameMode. You can also toggle this from a terminal with
      <code>grave gamewatch on|off</code>.</p>
    <div class="setrow"><button id="throttle-close">Got it</button></div>
  </div>
</div>
<div class="overlay" id="files-dlg" style="display:none">
  <div class="dlg" id="files-box">
    <button class="mini xbtn" id="files-x" title="close (Esc)" aria-label="close"
      style="position:absolute;top:10px;right:10px;z-index:2">✕</button>
    <h2 style="color:var(--ink);font-size:15px;margin-bottom:6px">📁 Files</h2>
    <div id="files-crumb" class="dim2"></div>
    <div id="files-drop"><div id="files-list"></div></div>
    <div class="setrow" style="margin-top:10px">
      <button class="mini" id="files-up">⬆ up</button>
      <button class="mini" id="files-mkdir">📂 new folder</button>
      <label class="mini abtn" style="cursor:pointer">⬆ upload<input type="file"
        id="files-upload" multiple style="display:none"></label>
      <span class="setlabel dim2" id="files-msg"></span>
    </div>
    <p class="dim2" style="font-size:11px;margin-top:4px">drag files onto the list to upload · confined to the appliance root</p>
  </div>
</div>
<div class="overlay" id="appframe" style="display:none">
  <div class="dlg" id="appframe-box">
    <div id="appframe-bar">
      <span id="appframe-title"></span>
      <a id="appframe-open" class="mini abtn" target="_blank" rel="noopener"
        title="open in a full window">↗ full</a>
      <button class="mini xbtn" id="appframe-x" title="close (Esc)" aria-label="close">✕</button>
    </div>
    <iframe id="appframe-if" src="about:blank" title="app"></iframe>
  </div>
</div>
<div id="panels">
  <div class="panel" data-panel="prs"><h2>🔀 Pull requests</h2><table id="prs"></table></div>
  <div class="panel" data-panel="ci"><h2>🏗️ CI status</h2><table id="ci"></table></div>
  <div class="panel" data-panel="linear"><h2>📐 Linear — assigned to me</h2><table id="linear"></table>
    <div class="setrow"><input id="new-linear" placeholder="new issue title…" style="flex:1">
      <button class="mini" id="add-linear">➕</button></div>
  </div>
  <div class="panel" data-panel="usage"><h2>🧾 Agent usage</h2><table id="usage"></table></div>
  <div class="panel" data-panel="tmux"><h2>🤖 Agent sessions — tap to open</h2><table id="tmux"></table></div>
  <div class="panel" data-panel="repos"><h2>📦 Repos</h2><table id="repos"></table></div>
  <div class="tiles w-full" data-panel="stats" id="tiles"></div>
  <div class="panel w-full" data-panel="actions"><h2>🕹️ Actions</h2>
    <div class="actions">
      <button data-act="gaming">🎮 Gaming mode</button>
      <button data-act="developer">💻 Developer mode</button>
      <button data-act="restart-t3" data-confirm="Restart T3 Code? Active agent sessions survive, the UI reconnects.">↻ Restart T3 Code</button>
      <button data-act="update-t3" data-confirm="Install the latest stable T3 Code release and restart its web service? Active agent sessions survive.">⬆ Update T3 Code</button>
      <button data-act="update-grave" data-confirm="Update gravedecay using this appliance's configured release/edge channel, then re-raise it? The dashboard will briefly reconnect; agent sessions survive.">⬆ Update gravedecay</button>
      <button data-act="doctor">🩺 Run doctor</button>
      <button data-act="reboot" data-confirm="Reboot the machine? Agent sessions die; everything else comes back automatically in the configured boot mode.">🔁 Reboot box</button>
      <button id="kill-open">🗡️ Kill sessions…</button>
    </div>
    <div class="setrow release-picker">
      <label for="grave-release">🪦 Release</label>
      <select id="grave-release" aria-label="gravedecay release">
        <option value="">load releases…</option>
      </select>
      <button class="mini" id="install-grave-release" disabled>⬆ Install selected</button>
      <span id="grave-release-state">Choose an exact stable release; config and agent sessions are preserved.</span>
    </div>
  </div>
  <div class="panel" data-panel="services"><h2>⚙️ Services</h2><table id="services"></table></div>
  <div class="panel" data-panel="docker"><h2>🐳 Docker</h2><table id="docker"></table></div>
  <div class="panel w-full" data-panel="journal"><h2>📋 Journal errors (24 h)</h2><pre id="journal"></pre></div>
</div>
<footer id="foot">🪦 gravedecay © <span id="footyear"></span> — <span id="epitaph"></span></footer>
<script>
const $=id=>document.getElementById(id);
// one epitaph per visit
$('footyear').textContent=new Date().getFullYear();
$('epitaph').textContent=[
  'the box never sleeps. neither do they.',
  'six feet down, five nines up.',
  'what is dead may never idle.',
  'your agents work the graveyard shift.',
  'rest in production.',
  'no rest for the deployed.',
  'dig once, ship forever.',
  'buried, not broken.',
][Math.floor(Math.random()*8)];
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
// same-origin app paths need the https origin spelled out when gravedecay is
// viewed on a bare port (localhost:4712) rather than mounted at /grave/
const appUrl=u=>(location.port&&location.port!=='443'&&u.startsWith('/'))
  ?`https://${location.hostname}${u}`:u;
// 'claude' or 'codex' if this tile launches that agent CLI via /term/?arg=…,
// else null. Gates the ⚡ skip-perms toggle and the -yolo session rewrite.
const agentArg=u=>{const m=/[?&]arg=(claude|codex)(?=$|&)/.exec(u||'');return m?m[1]:null;};
const PANEL_NAMES={prs:'Pull requests',ci:'CI status',
  linear:'Linear issues',usage:'Agent usage',tmux:'Agent sessions',repos:'Repos',
  stats:'Stats tiles',actions:'Actions',services:'Services',docker:'Docker',
  journal:'Journal errors'};
const PANEL_TABS={prs:'work',ci:'work',linear:'work',usage:'work',
  tmux:'work',repos:'work',
  stats:'system',actions:'system',services:'system',docker:'system',journal:'system'};
let linearConfigured=false,lastMode=null,lastTmux=[];
let cfg=null,cfgSrv='',envApps=[],layoutKey='';
let graveReleasesLoaded=false;
let pollFailures=0,lastConnected=0;
let activeTab=localStorage.getItem('grave-tab')||'work';
let scrollInput=0;
['touchstart','touchmove','wheel','pointerdown','keydown'].forEach(type=>
  addEventListener(type,()=>scrollInput++,{passive:true}));
// deep-link a tab: /grave/?tab=system (also handy for screenshots)
{const qp=new URLSearchParams(location.search).get('tab');
 if(qp==='work'||qp==='system')activeTab=qp;}
function allApps(){return envApps.concat(cfg&&cfg.custom_apps||[])}
function applyLayout(){
  const c=$('panels'),order=(cfg.panel_order||[]).slice();
  Object.keys(PANEL_NAMES).forEach(k=>{if(!order.includes(k))order.push(k)});
  order.forEach(id=>{
    const el=document.querySelector(`[data-panel="${id}"]`);
    if(!el)return;
    el.style.display=(cfg.hidden_panels.includes(id)||(PANEL_TABS[id]||'system')!==activeTab)
      ?'none':'';
    c.appendChild(el);
  });
  document.querySelectorAll('.tab').forEach(t=>
    t.classList.toggle('active',t.dataset.tab===activeTab));
}
document.querySelectorAll('.tab').forEach(t=>t.onclick=()=>{
  activeTab=t.dataset.tab;localStorage.setItem('grave-tab',activeTab);
  if(cfg)applyLayout();
  if(activeTab==='system')loadGraveReleases();
});
async function loadGraveReleases(force=false){
  if(graveReleasesLoaded&&!force)return;
  graveReleasesLoaded=true;
  const select=$('grave-release'),button=$('install-grave-release'),status=$('grave-release-state');
  status.textContent='fetching stable releases…';button.disabled=true;
  try{
    const r=await fetch('api/admin/releases',{cache:'no-store'}),data=await r.json();
    if(!r.ok)throw new Error(data.output||`HTTP ${r.status}`);
    select.innerHTML=(data.releases||[]).map(tag=>
      `<option value="${esc(tag)}"${tag===data.current?' selected':''}>${esc(tag)}${tag===data.current?' (installed)':''}</option>`).join('');
    if(!select.options.length)select.innerHTML='<option value="">no stable releases</option>';
    // WebKit retains the removed placeholder's empty value after innerHTML is
    // replaced; selected="..." on a new option is not enough in iOS Safari.
    if(data.current&&(data.releases||[]).includes(data.current))select.value=data.current;
    else if(select.options.length)select.selectedIndex=0;
    button.disabled=!select.value;
    status.textContent=`installed: ${data.current||data.checkout||'development checkout'}`;
  }catch(e){
    select.innerHTML='<option value="">release lookup failed</option>';
    status.textContent=e.message;button.disabled=true;
  }
}
$('grave-release').onchange=()=>{$('install-grave-release').disabled=!$('grave-release').value;};
$('install-grave-release').onclick=async()=>{
  const tag=$('grave-release').value;
  if(!tag||!confirm(`Install gravedecay ${tag} and re-raise the appliance?`))return;
  const button=$('install-grave-release'),status=$('grave-release-state');
  button.disabled=true;status.textContent=`queueing ${tag}…`;
  try{
    const r=await fetch('api/admin/upgrade',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({tag})}),data=await r.json();
    if(!r.ok)throw new Error(data.output||`HTTP ${r.status}`);
    status.textContent=`${tag} queued — reconnecting after raise…`;
  }catch(e){status.textContent=e.message;button.disabled=false;}
};
function render(s){
  // Replacing live regions above the viewport can make iOS WebKit discard its
  // document scroll anchor and jump to the top. Preserve the window position
  // explicitly across every poll-driven render. WebKit can adjust its anchor
  // on the next frame, so repeat then unless fresh input says the user moved.
  const scrollX=window.scrollX,scrollY=window.scrollY,inputAtRender=scrollInput;
  pollFailures=0;lastConnected=Date.now();paintConnection();
  envApps=s.apps||[];
  lastTmux=s.tmux||[];
  if(s.boot_mode){bootMode=s.boot_mode;paintBoot();}
  if(s.gamewatch){applyGamewatch(s.gamewatch);}
  // adopt settings saved elsewhere (another device/tab) — but never while
  // this device is mid-edit in the settings modal
  const sj=JSON.stringify(s.settings);
  if(!cfg){cfg=s.settings;cfgSrv=sj;schedule();}
  else if(sj!==cfgSrv&&$('settings-panel').style.display!=='block'){
    cfg=s.settings;cfgSrv=sj;layoutKey='';schedule();
  }
  const modeChanged=lastMode!==null&&lastMode!==s.mode;
  lastMode=s.mode;
  document.body.classList.toggle('gaming',s.mode==='gaming');
  $('game-banner').style.display=s.mode==='gaming'?'block':'none';
  if(s.mode==='gaming')
    $('torpor-line').textContent=s.torpor
      ?`dev stack buried · 🧊 ${s.torpor} agent session${s.torpor>1?'s':''} in torpor (RAM kept, zero CPU)`
      :'dev stack buried · no agent sessions held';
  if(modeChanged)schedule();
  const k=JSON.stringify([cfg.panel_order,cfg.hidden_panels,activeTab]);
  if(k!==layoutKey){layoutKey=k;applyLayout();}
  if(activeTab==='system')loadGraveReleases();
  // 📁 Files is a built-in tile: opens the native file-manager modal.
  const tiles=[{name:'📁 Files',url:FILES_URL}].concat(allApps());
  $('apps').innerHTML=tiles.filter(a=>!cfg.hidden_apps.includes(a.name)).map(a=>{
    if(a.url===FILES_URL)
      return `<a class="app" href="#files" data-files="1">${esc(a.name)}</a>`;
    // ⚡ skip-perms: a claude/codex /term tile launched with gates off routes
    // to the -yolo webterm session (which adds the dangerous flag).
    let url=a.url;
    if(agentArg(a.url)&&(cfg.yolo_apps||[]).includes(a.name))
      url=a.url.replace(/([?&]arg=)(claude|codex)(?=$|&)/,'$1$2-yolo');
    // modal mode is offered for every tile EXCEPT T3 (url '/'), which needs
    // the full window; new-tab always wins if both somehow got set.
    const newtab=(cfg.newtab_apps||[]).includes(a.name);
    const modal=!newtab&&a.url!=='/'&&(cfg.modal_apps||[]).includes(a.name);
    return `<a class="app" href="${esc(appUrl(url))}"${
      newtab?' target="_blank" rel="noopener"':''}${
      modal?` data-modal="${esc(url)}" data-modal-name="${esc(a.name)}"`:''
    }>${esc(a.name)}</a>`;
  }).join('');
  $('mode').textContent=(s.mode==='developer'?'💻 developer':'🎮 gaming');
  $('meta').textContent=`up ${fmtUp(s.system.uptime_s)}`;
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
      <td>${statusDot('active')}<a href="${esc(appUrl('/term/?arg='+encodeURIComponent(x.name)))}">${esc(x.name)}</a></td>
      <td class="num">${esc(x.windows)} win</td>
      <td class="dim">${esc(x.attached)}</td>
      <td class="num"><a class="kill" data-kill="${esc(x.name)}" title="kill session">✕</a></td></tr>`).join('')
    :'<tr><td class="dim">no agent sessions — use the Terminal/Claude/Codex tiles</td></tr>';
  $('repos').innerHTML=s.repos.length?s.repos.map(r=>`<tr>
      <td>${statusDot(r.dirty?'inactive':'active')}${esc(r.name)}</td>
      <td class="dim">${esc(r.branch)}${r.dirty?` · ${r.dirty} dirty`:''}</td>
      <td class="dim">${esc(r.last_when)}</td></tr>`).join('')
    :'<tr><td class="dim">no repos</td></tr>';
  $('journal').textContent=s.journal.join('\n');
  // integration lists: recent 5 inline, "show all" jumps to the real app
  const moreRow=(shown,total,url)=>total>shown&&url
    ? `<tr><td colspan="3"><a href="${esc(url)}" target="_blank" rel="noopener">show all (${total}${total>=15?'+':''}) →</a></td></tr>`:'';
  const gh=s.github||{};
  const anyMine=(gh.prs||[]).some(p=>p.mine);
  $('prs').innerHTML=gh.error
    ? `<tr><td class="dim">${esc(gh.error)}</td></tr>`
    : ((gh.prs||[]).slice(0,5).map(p=>`<tr>
        <td>${p.mine?'<span title="your review is requested">👀 </span>':''}<a href="${esc(p.url)}" target="_blank" rel="noopener">${esc(p.repo)} #${p.number}</a></td>
        <td class="dim">${esc(p.title)}</td></tr>`).join('')
       +moreRow(5,(gh.prs||[]).length,gh.more_url)
       +(anyMine?'<tr><td class="dim" colspan="2">👀 = your review requested</td></tr>':'')
       ||'<tr><td class="dim">no open PRs 🎉</td></tr>');
  $('ci').innerHTML=((s.ci||{}).rows||[]).map(r=>{
    const st=r.status!=='completed'?'inactive':(r.conclusion==='success'?'active':'failed');
    return `<tr><td>${statusDot(st)}<a href="${esc(r.url)}" target="_blank" rel="noopener">${esc(r.repo)}</a></td>
      <td class="dim">${esc(r.workflow)}</td>
      <td class="dim">${esc(r.branch)} · ${esc(r.conclusion||r.status)}</td></tr>`;
  }).join('')||'<tr><td class="dim">no workflow runs in any repo</td></tr>';
  const us=s.usage;
  if(us){
    const fk=n=>n>=1e6?(n/1e6).toFixed(1)+'M':n>=1e3?Math.round(n/1e3)+'k':''+n;
    const win=m=>m===300?'5h window':m===10080?'weekly':Math.round(m/60)+'h window';
    const rst=t=>t?new Date(t*1000).toLocaleString(undefined,{weekday:'short',hour:'2-digit',minute:'2-digit'}):'';
    const lim=(w,label)=>w&&w.pct!=null?`<tr><td class="dim">${label}</td>
      <td><div class="meter ${meterClass(w.pct)}" style="margin-top:5px"><i style="width:${Math.min(w.pct,100)}%"></i></div></td>
      <td class="num">${w.pct}%${w.resets_at?'<span class="dim"> · resets '+esc(rst(w.resets_at))+'</span>':''}</td></tr>`:'';
    const c=us.claude,x=us.codex,L=us.codex_limits;
    $('usage').innerHTML=
      `<tr><td>🤖 Claude 24h</td><td class="dim">${fk(c.today.in)} in · ${fk(c.today.out)} out · ${fk(c.today.cache)} cached</td><td class="num">≈$${c.today.cost.toFixed(2)}</td></tr>`
      +`<tr><td class="dim">&nbsp;&nbsp;7 days</td><td class="dim">${fk(c.week.in)} in · ${fk(c.week.out)} out · ${c.week.msgs} msgs</td><td class="num">≈$${c.week.cost.toFixed(2)}</td></tr>`
      +`<tr><td>🧠 Codex 24h</td><td class="dim">${fk(x.today.in)} in (${fk(x.today.cached)} cached) · ${fk(x.today.out)} out</td><td class="num">${x.today.sessions} sess</td></tr>`
      +`<tr><td class="dim">&nbsp;&nbsp;7 days</td><td class="dim">${fk(x.week.in)} in · ${fk(x.week.out)} out</td><td class="num">${x.week.sessions} sess</td></tr>`
      +(L?lim(L.primary,`codex ${win(L.primary.mins)}`)+lim(L.secondary,`codex ${win(L.secondary.mins)}`)
          +`<tr><td class="dim" colspan="3">plan: ${esc(L.plan||'?')} · $ = est. API value (subscriptions bill flat)</td></tr>`
        :'<tr><td class="dim" colspan="3">$ = est. API value (subscriptions bill flat)</td></tr>');
  }
  const li=s.linear||{};
  linearConfigured=!!li.configured;
  $('linear').innerHTML=!li.configured
    ? '<tr><td class="dim">add a Linear API key in ⚙️ settings</td></tr>'
    : li.error?`<tr><td class="dim">${esc(li.error)}</td></tr>`
    : ((li.issues||[]).slice(0,5).map(i=>`<tr>
        <td><a href="${esc(i.url)}" target="_blank" rel="noopener">${esc(i.id)}</a></td>
        <td class="dim">${esc(i.title)}</td>
        <td class="dim">${esc(i.state)}</td></tr>`).join('')
       +moreRow(5,(li.issues||[]).length,li.more_url)
       ||'<tr><td class="dim">nothing assigned 🎉</td></tr>');
  if(window.scrollX!==scrollX||window.scrollY!==scrollY)
    window.scrollTo(scrollX,scrollY);
  requestAnimationFrame(()=>{
    if(scrollInput===inputAtRender&&(window.scrollX!==scrollX||window.scrollY!==scrollY))
      window.scrollTo(scrollX,scrollY);
  });
}
async function poll(){
  if(document.hidden)return;
  try{
    const r=await fetch('api/state');
    if(!r.ok)throw new Error(`HTTP ${r.status}`);
    render(await r.json());
  }catch(e){pollFailures++;paintConnection();}
}
function paintConnection(){
  const offline=!navigator.onLine,failed=pollFailures>0,c=$('connection');
  c.classList.toggle('show',offline||failed);
  if(!offline&&!failed)return;
  const ago=lastConnected?Math.max(1,Math.round((Date.now()-lastConnected)/1000)):null;
  $('connection-text').textContent=offline?'network offline — check Tailscale'
    :`box unreachable${ago?` · last connected ${ago}s ago`:''}`;
}
$('connection-retry').onclick=poll;
addEventListener('online',poll);addEventListener('offline',paintConnection);
// ---------- boot console (fetch-streamed SSE) ----------
// fetch instead of EventSource: real HTTP errors become readable (through
// EventSource a 403 and a dead box look identical) and there is no browser
// auto-retry that could silently re-trigger a mode switch.
let streaming=false,aborter=null,runId=0;
function closeConsole(){
  // closing only detaches the console — the action always finishes on the box
  if(aborter){aborter.abort();aborter=null;}
  $('console').style.display='none';
  unlockBody();
}
async function runStream(act,title){
  if(streaming)return;
  streaming=true;
  const myRun=++runId;
  aborter=new AbortController();
  $('game-confirm').style.display='none';
  $('console').style.display='block';lockBody();
  $('console-title').textContent='▚ '+(title||('grave '+act));
  const out=$('console-out');out.innerHTML='';
  $('console-close').style.display='none';
  const add=(t,cls)=>{const d=document.createElement('div');
    d.className=cls||(t.includes('✗')?'err':(/🎮|💻|🪦|Gaming|Developer/.test(t)?'hl':''));
    // token/URL lines get a copy button and a real link — selecting text on
    // a phone is misery, and secrets must be copyable in one tap
    const m=/^(Token|Pair URL): (\S+)$/.exec(t||'');
    if(m){
      const v=m[2],isUrl=m[1]==='Pair URL';
      d.innerHTML=esc(m[1])+': '
        +(isUrl?`<a href="${esc(v)}">${esc(v)}</a>`:`<b>${esc(v)}</b>`)
        +` <button class="mini copybtn" data-copy="${esc(v)}">📋 copy</button>`;
    }else d.textContent=t||' ';
    out.appendChild(d);out.scrollTop=out.scrollHeight;};
  let gotData=false,okDone=false;
  try{
    const r=await fetch('api/action-stream?action='+encodeURIComponent(act),
      {signal:aborter.signal});
    if(!r.ok){
      let msg=await r.text();
      try{msg=JSON.parse(msg).output||msg;}catch(_){}
      add(`— HTTP ${r.status}: ${msg}`,'err');
    }else{
      const rd=r.body.getReader(),dec=new TextDecoder();
      let buf='',finished=false;
      for(;!finished;){
        const{done,value}=await rd.read();
        if(done)break;
        buf+=dec.decode(value,{stream:true});
        let i;
        while((i=buf.indexOf('\n\n'))>=0){
          const chunk=buf.slice(0,i);buf=buf.slice(i+2);
          const ev=/^event: (.*)$/m.exec(chunk);
          const da=/^data: (.*)$/m.exec(chunk);
          if(ev&&ev[1]==='done'){
            const rc=da?da[1]:'?';
            okDone=(rc==='0');
            add(okDone?'— sequence complete ✓':'— exited with code '+rc,okDone?'hl':'err');
            finished=true;  // don't wait for EOF — proxies/Safari may hold it
          }else if(da){gotData=true;add(JSON.parse(da[1]));}
        }
      }
      try{rd.cancel();}catch(_){}
      if(!gotData&&!finished)add('— connection closed before any output arrived','err');
    }
  }catch(e){
    if(e.name!=='AbortError')
      add(gotData?'— stream lost: '+e+' (the action keeps running on the box)'
                 :'— request failed: '+e,'err');
  }
  streaming=false;aborter=null;
  $('console-close').style.display='';
  poll();
  // success: linger briefly so the ✓ registers, then get out of the way —
  // EXCEPT for output the user needs to read or copy (tokens, doctor)
  const KEEP_OPEN=['t3-pair','doctor','update-t3'];
  if(okDone&&!KEEP_OPEN.includes(act))
    setTimeout(()=>{if(runId===myRun)closeConsole();},3500);
}
$('console-out').addEventListener('click',async e=>{
  const v=e.target.dataset&&e.target.dataset.copy;
  if(!v)return;
  try{await navigator.clipboard.writeText(v);e.target.textContent='✓ copied';}
  catch(_){e.target.textContent='copy failed';}
});
$('console-x').onclick=closeConsole;
$('console').addEventListener('click',e=>{if(e.target.id==='console')closeConsole();});
$('game-confirm').addEventListener('click',e=>{
  if(e.target.id==='game-confirm'){e.currentTarget.style.display='none';unlockBody();}});
document.addEventListener('keydown',e=>{if(e.key==='Escape'){
  closeConsole();$('game-confirm').style.display='none';
  $('settings-panel').style.display='none';$('kill-dlg').style.display='none';
  $('throttle-dlg').style.display='none';
  closeFiles();closeAppModal();
  unlockBody();}});
$('console-close').onclick=()=>{$('console').style.display='none'};
// ---------- gaming confirm dialog ----------
function openGameConfirm(){
  $('gc-sessions').innerHTML=lastTmux.length
    ?`Agent sessions: <b>${lastTmux.map(x=>esc(x.name)).join(', ')}</b><br>
      <span class="dim2">🧊 keeps them frozen in RAM (zero CPU, thawed on wake) · ☠️ destroys them for maximum free RAM</span>`
    :'<span class="dim2">No agent sessions running — both options behave the same.</span>';
  $('game-confirm').style.display='block';lockBody();
}
$('gc-freeze').onclick=()=>runStream('gaming','burial sequence — freeze');
$('gc-kill').onclick=()=>runStream('gaming-kill','burial sequence — full kill');
$('gc-cancel').onclick=()=>{$('game-confirm').style.display='none';unlockBody();};
$('wake').onclick=()=>runStream('developer','startup sequence');
document.querySelectorAll('button[data-act]').forEach(b=>b.onclick=()=>{
  const act=b.dataset.act;
  if(act==='gaming'){openGameConfirm();return;}
  if(b.dataset.confirm&&!confirm(b.dataset.confirm))return;
  runStream(act);
});
// tap a session name to open it in the terminal; ✕ kills it (event
// delegation — rows are rebuilt every poll)
$('tmux').addEventListener('click',async e=>{
  const n=e.target.dataset&&e.target.dataset.kill;
  if(!n)return;
  e.preventDefault();
  if(!confirm(`Kill session "${n}"? Anything running in it dies.`))return;
  try{
    const r=await fetch('api/session-kill',{method:'POST',
      headers:{'Content-Type':'application/json'},body:JSON.stringify({name:n})});
    const j=await r.json();
    if(!j.ok)alert(j.output||'kill failed');
  }catch(err){alert('kill failed: '+err);}
  poll();
});
// linear quick-create
$('add-linear').onclick=async()=>{
  const t=$('new-linear').value.trim();
  if(!t)return;
  $('add-linear').disabled=true;
  try{
    const r=await fetch('api/linear-issue',{method:'POST',
      headers:{'Content-Type':'application/json'},body:JSON.stringify({title:t})});
    const j=await r.json();
    if(j.ok){$('new-linear').value='';poll();}
    else alert(j.output||'create failed');
  }catch(err){alert('create failed: '+err);}
  $('add-linear').disabled=false;
};
$('new-linear').addEventListener('keydown',e=>{if(e.key==='Enter')$('add-linear').click()});
// mode badge = one-tap mode flip (delegates to the action button, incl. confirm)
$('mode').onclick=()=>{
  const target=lastMode==='developer'?'gaming':'developer';
  const b=document.querySelector(`[data-act="${target}"]`);
  if(b&&!b.disabled)b.click();
};
// ---------- settings panel ----------
let draft=null;
function buildSettings(existing){
  draft=existing||JSON.parse(JSON.stringify(cfg));
  const w=$('set-widgets');
  const order=draft.panel_order.slice();
  Object.keys(PANEL_NAMES).forEach(k=>{if(!order.includes(k))order.push(k)});
  draft.panel_order=order;
  w.innerHTML=order.map((id,i)=>`<div class="setrow">
    <input type="checkbox" data-panel-vis="${id}" ${draft.hidden_panels.includes(id)?'':'checked'}>
    <span class="setlabel">${esc(PANEL_NAMES[id]||id)}
      <span style="color:var(--muted)">· ${PANEL_TABS[id]==='work'?'🛠️':'📟'}</span></span>
    <button class="mini" data-up="${i}" ${i===0?'disabled':''}>↑</button>
    <button class="mini" data-down="${i}" ${i===order.length-1?'disabled':''}>↓</button>
  </div>`).join('');
  w.querySelectorAll('[data-up]').forEach(b=>b.onclick=()=>{const i=+b.dataset.up;
    syncVis();
    [draft.panel_order[i-1],draft.panel_order[i]]=[draft.panel_order[i],draft.panel_order[i-1]];
    buildSettings(draft);});
  w.querySelectorAll('[data-down]').forEach(b=>b.onclick=()=>{const i=+b.dataset.down;
    syncVis();
    [draft.panel_order[i+1],draft.panel_order[i]]=[draft.panel_order[i],draft.panel_order[i+1]];
    buildSettings(draft);});
  const ap=$('set-apps');
  // list must reflect the DRAFT being edited, not the last-saved cfg —
  // otherwise a freshly added tile never shows up until after a save
  const draftApps=envApps.concat(draft.custom_apps||[]);
  ap.innerHTML=`<div class="setrow" style="color:var(--muted)">
      <span style="width:16px">👁</span><span class="setlabel">tile</span>
      <span title="claude/codex only: run with permission gates OFF">⚡</span>
      <span title="open in a modal on the dashboard">▢ modal</span>
      <span title="open in a new tab instead of inside the PWA">↗ new tab</span></div>`
    +draftApps.map((a,i)=>`<div class="setrow">
    <input type="checkbox" data-app-vis="${esc(a.name)}" ${draft.hidden_apps.includes(a.name)?'':'checked'}>
    <span class="setlabel">${esc(a.name)} <span style="color:var(--muted)">${esc(a.url)}</span></span>
    ${agentArg(a.url)
      ?`<label title="DANGER: run ${agentArg(a.url)} with all permission/approval gates off (claude --dangerously-skip-permissions / codex --dangerously-bypass-approvals-and-sandbox)"><input type="checkbox" data-app-yolo="${esc(a.name)}" ${(draft.yolo_apps||[]).includes(a.name)?'checked':''}> ⚡</label>`
      :`<span class="dim2" title="only claude/codex tiles">·</span>`}
    ${a.url==='/'
      ?`<span class="dim2" title="T3 needs the full window — no modal">▢ —</span>`
      :`<label title="open in a modal on the dashboard"><input type="checkbox" data-app-modal="${esc(a.name)}" ${(draft.modal_apps||[]).includes(a.name)?'checked':''}> ▢</label>`}
    <label title="open in a new tab"><input type="checkbox" data-app-newtab="${esc(a.name)}" ${(draft.newtab_apps||[]).includes(a.name)?'checked':''}> ↗</label>
    ${i>=envApps.length?`<button class="mini" data-del-app="${i-envApps.length}">✕</button>`:''}
  </div>`).join('');
  // modal and new-tab are mutually exclusive per tile — checking one clears the
  // other (matched by name in JS to dodge attribute-selector escaping on emoji)
  ap.querySelectorAll('[data-app-modal]').forEach(c=>c.addEventListener('change',()=>{
    if(!c.checked)return;
    ap.querySelectorAll('[data-app-newtab]').forEach(n=>{
      if(n.dataset.appNewtab===c.dataset.appModal)n.checked=false;});}));
  ap.querySelectorAll('[data-app-newtab]').forEach(c=>c.addEventListener('change',()=>{
    if(!c.checked)return;
    ap.querySelectorAll('[data-app-modal]').forEach(m=>{
      if(m.dataset.appModal===c.dataset.appNewtab)m.checked=false;});}));
  ap.querySelectorAll('[data-del-app]').forEach(b=>b.onclick=()=>{
    syncVis();draft.custom_apps.splice(+b.dataset.delApp,1);buildSettings(draft);});
  $('set-poll').value=String(draft.poll_ms);
  $('linear-state').textContent=linearConfigured?'✓ configured':'(not set)';
  document.querySelectorAll('[data-auth]').forEach(a=>
    a.href=appUrl(`/term/?arg=${a.dataset.auth}`));
}
function syncVis(){
  draft.hidden_panels=[...document.querySelectorAll('[data-panel-vis]')]
    .filter(c=>!c.checked).map(c=>c.dataset.panelVis);
  draft.hidden_apps=[...document.querySelectorAll('[data-app-vis]')]
    .filter(c=>!c.checked).map(c=>c.dataset.appVis);
  draft.modal_apps=[...document.querySelectorAll('[data-app-modal]')]
    .filter(c=>c.checked).map(c=>c.dataset.appModal);
  draft.yolo_apps=[...document.querySelectorAll('[data-app-yolo]')]
    .filter(c=>c.checked).map(c=>c.dataset.appYolo);
  draft.newtab_apps=[...document.querySelectorAll('[data-app-newtab]')]
    .filter(c=>c.checked).map(c=>c.dataset.appNewtab);
  draft.poll_ms=+$('set-poll').value;
}
// body scroll-lock while any overlay is open — without this, iOS scrolls
// the page underneath the modal ("intermixed" scrolling)
const lockBody=()=>{document.body.style.overflow='hidden'};
const unlockBody=()=>{
  if(![...document.querySelectorAll('.overlay')].some(o=>o.style.display==='block'))
    document.body.style.overflow='';
};
document.querySelectorAll('.sec-toggle').forEach(h=>h.onclick=()=>{
  const sec=$(h.dataset.sec);
  const open=sec.style.display!=='none';
  sec.style.display=open?'none':'block';
  h.textContent=(open?'▸':'▾')+h.textContent.slice(1);
});
function closeSettings(){$('settings-panel').style.display='none';unlockBody();}
$('gear').onclick=()=>{
  const p=$('settings-panel');
  if(p.style.display==='block'){closeSettings();return;}
  if(!cfg)return;
  buildSettings();
  // sections start collapsed — the modal opens phone-sized
  document.querySelectorAll('.sec-toggle').forEach(h=>{
    $(h.dataset.sec).style.display='none';
    h.textContent='▸'+h.textContent.slice(1);
  });
  p.style.display='block';p.scrollTop=0;lockBody();
};
$('close-set').onclick=closeSettings;
$('settings-x').onclick=closeSettings;
$('settings-panel').addEventListener('click',e=>{
  if(e.target.id==='settings-panel')closeSettings();});
// ---------- kill-sessions dialog ----------
function buildKillList(){
  $('kill-list').innerHTML=lastTmux.length?lastTmux.map(x=>`<div class="setrow">
    <span class="setlabel">🤖 ${esc(x.name)} <span class="dim2">· ${esc(x.windows)} win · ${esc(x.attached)}</span></span>
    <button class="mini" data-kill-one="${esc(x.name)}">✕ kill</button>
  </div>`).join('')
  :'<div class="setrow dim2">no sessions running</div>';
}
async function killSession(n){
  const r=await fetch('api/session-kill',{method:'POST',
    headers:{'Content-Type':'application/json'},body:JSON.stringify({name:n})});
  return (await r.json()).ok;
}
$('kill-open').onclick=()=>{buildKillList();$('kill-msg').textContent='';$('kill-dlg').style.display='block';lockBody();};
$('kill-x').onclick=()=>{$('kill-dlg').style.display='none';unlockBody();};
$('kill-dlg').addEventListener('click',async e=>{
  if(e.target.id==='kill-dlg'){e.currentTarget.style.display='none';unlockBody();return;}
  const n=e.target.dataset&&e.target.dataset.killOne;
  if(!n)return;
  e.target.disabled=true;
  const ok=await killSession(n);
  $('kill-msg').textContent=ok?`killed ${n}`:'kill failed';
  lastTmux=lastTmux.filter(x=>x.name!==n);buildKillList();poll();
});
$('kill-all').onclick=async()=>{
  if(!lastTmux.length){$('kill-msg').textContent='nothing to kill';return;}
  if(!confirm(`Kill ALL ${lastTmux.length} session(s)?`))return;
  for(const x of lastTmux)await killSession(x.name);
  $('kill-msg').textContent='all sessions killed ☠️';
  lastTmux=[];buildKillList();poll();
};
$('t3-pair-btn').onclick=()=>runStream('t3-pair','t3 pairing token — enter it on the new device (15 min)');
// boot mode toggle (quick POST, not a console stream)
let bootMode=null;
function paintBoot(){
  $('boot-dev').classList.toggle('activebtn',bootMode==='developer');
  $('boot-game').classList.toggle('activebtn',bootMode==='gaming');
}
async function setBoot(m){
  $('boot-state').textContent='saving…';
  try{
    const r=await fetch('api/action',{method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({action:'bootmode-'+m})});
    const j=await r.json();
    $('boot-state').textContent=j.ok?'saved ✓':(j.output||'failed');
    if(j.ok){bootMode=m;paintBoot();}
  }catch(e){$('boot-state').textContent='failed: '+e;}
}
$('boot-dev').onclick=()=>setBoot('developer');
$('boot-game').onclick=()=>setBoot('gaming');
// game-mode auto-throttle: info modal + on/off toggle (Steam Machine only —
// the section stays hidden unless the watcher service is installed)
let throttleOn=null;
function paintThrottle(){
  $('throttle-on').classList.toggle('activebtn',throttleOn===true);
  $('throttle-off').classList.toggle('activebtn',throttleOn===false);
}
function applyGamewatch(g){
  const show=!!g.installed;
  $('throttle-head').style.display=show?'':'none';
  $('throttle-row').style.display=show?'':'none';
  if(show){throttleOn=g.on;paintThrottle();
    if(g.on&&!g.running)$('throttle-state').textContent='on (watcher stopped?)';}
}
async function setThrottle(on){
  $('throttle-state').textContent='saving…';
  try{
    const r=await fetch('api/action',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({action:on?'gamewatch-on':'gamewatch-off'})});
    const j=await r.json();
    $('throttle-state').textContent=j.ok?'saved ✓':(j.output||'failed');
    if(j.ok){throttleOn=on;paintThrottle();}
  }catch(e){$('throttle-state').textContent='failed: '+e;}
}
$('throttle-on').onclick=()=>setThrottle(true);
$('throttle-off').onclick=()=>setThrottle(false);
function closeThrottle(){$('throttle-dlg').style.display='none';unlockBody();}
$('throttle-info').onclick=()=>{$('throttle-dlg').style.display='block';lockBody();};
$('throttle-x').onclick=closeThrottle;
$('throttle-close').onclick=closeThrottle;
$('throttle-dlg').addEventListener('click',e=>{if(e.target.id==='throttle-dlg')closeThrottle();});
$('add-app').onclick=()=>{
  const n=$('new-app-name').value.trim(),u=$('new-app-url').value.trim();
  if(!u){$('set-msg').textContent='tile needs a URL';return;}
  syncVis();draft.custom_apps.push({name:n||u,url:u});
  $('new-app-name').value='';$('new-app-url').value='';
  $('set-msg').textContent='tile added — hit 💾 Save';
  buildSettings(draft);
};
$('new-app-url').addEventListener('keydown',e=>{if(e.key==='Enter')$('add-app').click()});
$('save-set').onclick=async()=>{
  syncVis();
  const payload={...draft};
  const lk=$('set-linear').value.trim();
  if(lk)payload.linear_key=lk;
  $('set-msg').textContent='saving…';
  try{
    const r=await fetch('api/settings',{method:'POST',
      headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});
    const j=await r.json();
    if(j.ok){cfg=j.settings;cfgSrv=JSON.stringify(j.settings);layoutKey='';schedule();
      linearConfigured=!!j.linear_configured;
      $('set-linear').value='';
      $('linear-state').textContent=linearConfigured?'✓ configured':'(not set)';
      $('set-msg').textContent='saved ✓';poll();}
    else $('set-msg').textContent=j.output||'save failed';
  }catch(e){$('set-msg').textContent='save failed: '+e;}
};
// ---------- file manager + app-in-modal ----------
const FILES_URL='grave:files';
let filesPath='';
// launcher clicks: 📁 Files opens the native modal, modal-tiles open an iframe
$('apps').addEventListener('click',e=>{
  const a=e.target.closest('a');if(!a)return;
  if(a.dataset.files){e.preventDefault();openFiles();return;}
  if(a.dataset.modal){e.preventDefault();openAppModal(a.dataset.modal,a.dataset.modalName);}
});
function fmtSize(n){return n<1024?n+' B':n<1048576?(n/1024).toFixed(0)+' K':(n/1048576).toFixed(1)+' M';}
function joinPath(a,b){return a?a+'/'+b:b;}
function openFiles(){$('files-dlg').style.display='block';lockBody();loadFiles('');}
function closeFiles(){$('files-dlg').style.display='none';unlockBody();}
async function fsOp(body){
  const r=await fetch('api/fs',{method:'POST',
    headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
  return r.json();
}
async function loadFiles(path){
  $('files-msg').textContent='';
  let j;
  try{const r=await fetch('api/files?path='+encodeURIComponent(path));j=await r.json();}
  catch(e){$('files-msg').textContent='load failed: '+e;return;}
  if(!j.ok){$('files-msg').textContent=j.output||'load failed';return;}
  filesPath=j.path;
  $('files-up').disabled=!j.path;
  // breadcrumb: root + each ancestor is clickable
  let acc='',crumb=`<a data-cd="">${esc(j.root)}</a>`;
  (j.path?j.path.split('/'):[]).forEach(seg=>{
    acc=acc?acc+'/'+seg:seg;crumb+=' / <a data-cd="'+esc(acc)+'">'+esc(seg)+'</a>';});
  $('files-crumb').innerHTML=crumb;
  $('files-list').innerHTML=j.entries.length?j.entries.map(en=>{
    const icon=en.type==='dir'?(en.link?'🔗':'📁'):'📄';
    return `<div class="frow" data-type="${en.type}" data-name="${esc(en.name)}">
      <span>${icon}</span><span class="fname">${esc(en.name)}</span>
      <span class="fmeta">${en.type==='dir'?'':fmtSize(en.size)}</span>
      ${en.type==='file'?`<a class="mini" data-dl="${esc(en.name)}" title="download">⬇</a>`:''}
      <button class="mini" data-ren="${esc(en.name)}" title="rename">✏️</button>
      <button class="mini" data-rm="${esc(en.name)}" title="delete">🗑</button>
    </div>`;}).join(''):'<div id="files-empty">empty folder</div>';
}
$('files-crumb').addEventListener('click',e=>{
  const a=e.target.closest('[data-cd]');if(!a)return;
  e.preventDefault();loadFiles(a.dataset.cd);});
$('files-up').onclick=()=>{if(!filesPath)return;
  const i=filesPath.lastIndexOf('/');loadFiles(i<0?'':filesPath.slice(0,i));};
$('files-mkdir').onclick=async()=>{
  const n=prompt('New folder name:');if(!n)return;
  const j=await fsOp({op:'mkdir',path:filesPath,name:n});
  $('files-msg').textContent=j.output||'';if(j.ok)loadFiles(filesPath);};
$('files-list').addEventListener('click',async e=>{
  const dl=e.target.closest('[data-dl]');
  if(dl){const n=dl.dataset.dl,a=document.createElement('a');
    a.href='api/download?path='+encodeURIComponent(joinPath(filesPath,n));a.download=n;
    document.body.appendChild(a);a.click();a.remove();return;}
  const ren=e.target.closest('[data-ren]');
  if(ren){const n=ren.dataset.ren,nn=prompt('Rename to:',n);
    if(!nn||nn===n)return;
    const j=await fsOp({op:'rename',path:joinPath(filesPath,n),name:nn});
    $('files-msg').textContent=j.output||'';if(j.ok)loadFiles(filesPath);return;}
  const rm=e.target.closest('[data-rm]');
  if(rm){const n=rm.dataset.rm;
    if(!confirm('Delete "'+n+'"? This cannot be undone.'))return;
    const j=await fsOp({op:'delete',path:joinPath(filesPath,n)});
    $('files-msg').textContent=j.output||'';if(j.ok)loadFiles(filesPath);return;}
  const row=e.target.closest('.frow');
  if(row&&row.dataset.type==='dir')loadFiles(joinPath(filesPath,row.dataset.name));
});
async function uploadFiles(list){
  for(const f of list){
    $('files-msg').textContent='uploading '+f.name+'…';
    let j;
    try{const r=await fetch('api/upload?path='+encodeURIComponent(filesPath)
        +'&name='+encodeURIComponent(f.name),{method:'POST',body:f});j=await r.json();}
    catch(e){$('files-msg').textContent='upload failed: '+e;return;}
    if(!j.ok){$('files-msg').textContent=j.output||'upload failed';return;}
  }
  $('files-msg').textContent='uploaded ✓';loadFiles(filesPath);
}
$('files-upload').addEventListener('change',e=>{
  if(e.target.files.length)uploadFiles(e.target.files);e.target.value='';});
$('files-x').onclick=closeFiles;
$('files-dlg').addEventListener('click',e=>{if(e.target.id==='files-dlg')closeFiles();});
{const drop=$('files-drop');
 ['dragenter','dragover'].forEach(ev=>drop.addEventListener(ev,e=>{
   e.preventDefault();drop.classList.add('drag');}));
 drop.addEventListener('dragleave',e=>{if(e.target===drop)drop.classList.remove('drag');});
 drop.addEventListener('drop',e=>{e.preventDefault();drop.classList.remove('drag');
   if(e.dataTransfer.files.length)uploadFiles(e.dataTransfer.files);});}
// app-in-modal (iframe over the dashboard) — never used for T3
function openAppModal(url,name){
  const full=appUrl(url);
  $('appframe-title').textContent=name||url;
  $('appframe-open').href=full;
  $('appframe-if').src=full;
  $('appframe').style.display='block';lockBody();
}
function closeAppModal(){
  $('appframe').style.display='none';$('appframe-if').src='about:blank';unlockBody();}
$('appframe-x').onclick=closeAppModal;
$('appframe').addEventListener('click',e=>{if(e.target.id==='appframe')closeAppModal();});
// ---------- boot ----------
let timer=null;
function schedule(){clearInterval(timer);
  // game mode: back off to 30s — the dashboard must not cost resources
  timer=setInterval(poll,lastMode==='gaming'?30000:((cfg&&cfg.poll_ms)||5000));}
const BOOT=/*BOOT*/null;   // server-rendered initial state: instant first paint
if(BOOT)render(BOOT);else poll();
schedule();
document.addEventListener('visibilitychange',()=>{if(!document.hidden)poll()});
// Own the entire appliance origin so /grave/, T3, /term/ and /pair/ remain in
// one installed app.  The worker only supplies an offline navigation page;
// state and action responses are always live and never cached.
if('serviceWorker'in navigator)
  addEventListener('load',()=>navigator.serviceWorker.register('sw.js',{scope:'/'}).catch(()=>{}));
</script></body></html>
""".replace("@HOST@", HOST).replace("@BASE@", BASE or "/grave")

if __name__ == "__main__":
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
