#!/usr/bin/env python3
# gravedecay — status dashboard for a gravedecay appliance.
# Stdlib only. The PWA shell lives in static/ (installed as dashboard-static/).
# Binds 127.0.0.1:$GRAVEDECAY_PORT (gravedecay.service), published tailnet-only
# via `tailscale serve`. Reads host state directly (systemd, docker,
# tmux -L agents, git, sensors, journald) — which is why this is a host
# service, not a container.
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

import base64
import binascii
import concurrent.futures
import functools
import glob
import hmac
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
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(os.environ.get("GRAVEDECAY_PORT", os.environ.get("DASH_PORT", "4712")))
GRAVE_ROOT = os.environ.get("GRAVE_ROOT", "/srv/dev")
PLATFORM = os.environ.get("GRAVEDECAY_PLATFORM", "linux").lower()
MACOS = PLATFORM == "macos"
# Opt-in macOS agents layer (macos/install.sh --agents): the Mac serves T3
# and the web terminal, so exactly the identity-gated pairing/session subset
# of the endpoint allowlist reopens. Set only by the installer's LaunchAgent.
MACOS_AGENTS = MACOS and os.environ.get("GRAVEDECAY_MACOS_AGENTS") == "1"
PORTABLE = PLATFORM in ("container", "portable")
BIND_HOST = "0.0.0.0" if PORTABLE else "127.0.0.1"
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
# Set only on multi-user backends by root-owned EnvironmentFile= entries.  A
# loopback TCP client cannot forge this header; /healthz remains the sole
# unauthenticated maintenance endpoint.
BACKEND_TOKEN = os.environ.get("GRAVEDECAY_BACKEND_TOKEN", "")
REQUIRE_BACKEND_TOKEN = os.environ.get("GRAVEDECAY_REQUIRE_BACKEND_TOKEN") == "1"
UNITS = [u for u in os.environ.get(
    "GRAVEDECAY_UNITS", "t3code,gravedecay,gravedecay-term,tailscaled,sshd,docker").split(",") if u]
APPS = [{"name": name.strip(), "url": url.strip()}
        for name, _, url in (p.partition("=") for p in os.environ.get(
            "GRAVEDECAY_APPS", "⌨️ T3 Code=/").split(";"))
        if url.strip()]
if MACOS and "GRAVEDECAY_APPS" not in os.environ:
    APPS = [{"name": "📡 Network", "url": "/net/"}]
# User preferences, editable from the ⚙️ panel (writes gated to ALLOWED_USERS
# exactly like actions). Stored beside the other appliance config.
SETTINGS_PATH = os.path.join(GRAVE_ROOT, "config", "gravedecay-settings.json")
DEFAULT_SETTINGS = {
    "panel_order": ["prs", "linear", "ci", "tmux", "sessions", "usage",
                    "inbox", "repos",
                    "stats", "actions", "services", "docker", "journal"],
    "hidden_panels": [],   # panel ids to hide
    "hidden_apps": [],     # launcher tile names to hide
    "newtab_apps": [],     # tile names that open in a new tab instead of in-PWA
    "modal_apps": [],      # tile names that open in an iframe modal on the dashboard
    "yolo_apps": [],       # claude/codex tiles launched with permission gates OFF
    "custom_apps": [],     # extra tiles: [{"name": ..., "url": ...}]
    "t3_tile": "pwa",      # T3 tile opens: "pwa" (web UI in-app) | "app"
                           # (hand off to the official T3 Code app, t3code://)
    "poll_ms": 5000,       # dashboard refresh interval
}
# The Mac companion deliberately keeps its project inventory outside its
# Application Support root.  Expand this at startup so the settings UI shows a
# useful, absolute default without requiring shell expansion from a browser.
MACOS_REPO_ROOT_DEFAULT = os.path.realpath(os.path.expanduser("~/Sites"))
if MACOS:
    DEFAULT_SETTINGS["repo_root"] = MACOS_REPO_ROOT_DEFAULT


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


def public_scheme(headers):
    """Use Serve's forwarded HTTPS scheme, while localhost stays HTTP."""
    scheme = headers.get("X-Forwarded-Proto", "").split(",", 1)[0].strip().lower()
    if scheme in ("http", "https"):
        return scheme
    return "https" if headers.get("Tailscale-User-Login") else "http"


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
    if s["t3_tile"] not in ("pwa", "app"):
        s["t3_tile"] = "pwa"
    return s


def save_settings(data):
    merged = load_settings()
    for k, default in DEFAULT_SETTINGS.items():
        if k in data and isinstance(data[k], type(default)):
            merged[k] = data[k]
    merged["poll_ms"] = max(2000, min(60000, int(merged["poll_ms"])))
    merged["custom_apps"] = _sanitize_custom_apps(merged["custom_apps"])
    if merged["t3_tile"] not in ("pwa", "app"):
        merged["t3_tile"] = "pwa"
    tmp = SETTINGS_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(merged, f, indent=2)
    os.replace(tmp, SETTINGS_PATH)
    return merged


def settings_response(merged):
    return {"ok": True, "settings": merged,
            "linear_configured": bool(linear_key()),
            "notify": None if PORTABLE else notify_state()}


def macos_repo_root(value=None):
    """Canonical Mac project root, or a clear validation error.

    The root is supplied by an owner in Settings, so never let it become an
    implicit relative path or a shell fragment.  ``realpath`` also means a
    symlinked root is checked against the directory it actually names.
    """
    root = MACOS_REPO_ROOT_DEFAULT if value is None else str(value).strip()
    if not os.path.isabs(root):
        return None, "Repository root must be an absolute path"
    root = os.path.realpath(root)
    if not os.path.isdir(root):
        return None, f"Repository root does not exist or is not a directory: {root}"
    return root, None


GRAVE = os.environ.get("GRAVEDECAY_GRAVE", "/usr/local/bin/grave")
MACOS_GRAVE = os.path.join(GRAVE_ROOT, "scripts", "grave")
# On a managed-toolchain host (SteamOS) t3 shares grave's durable bin dir
# (~/.local/bin), but package hosts diverge: grave installs to /usr/local/bin
# while npm puts t3 in /usr/bin, so grave's sibling alone is not enough.
# Resolution order: explicit override, grave's sibling, then PATH (raise.sh
# threads the toolchain bin dirs into every service's PATH).


def _resolve_t3():
    override = os.environ.get("GRAVEDECAY_T3")
    if override:
        return override
    sibling = os.path.join(os.path.dirname(GRAVE), "t3")
    if os.path.exists(sibling):
        return sibling
    return shutil.which("t3") or sibling


T3 = _resolve_t3()
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
    # non-interactive Connect teardown; enabling (publish/full) is an OAuth
    # flow, so those run in the web terminal (auth-t3publish / auth-t3full)
    "t3connect-off": [GRAVE, "t3", "connect", "off"],
    "reboot": ["sudo", "-n", "systemctl", "reboot"],
    "bootmode-developer": [GRAVE, "bootmode", "developer"],
    "bootmode-gaming": [GRAVE, "bootmode", "gaming"],
    "gamewatch-on": [GRAVE, "gamewatch", "on"],    # auto-throttle: freeze on game launch
    "gamewatch-off": [GRAVE, "gamewatch", "off"],
    "keepalive-on": [GRAVE, "keepalive", "on"],    # 🟢 "always alive": warm tailnet relay paths
    "keepalive-off": [GRAVE, "keepalive", "off"],
}
# The source macOS companion is intentionally observability-only.  Keep this
# gate server-side: hiding buttons is not an authorization boundary.  The
# opt-in agents layer reopens exactly pairing — no sudo/systemd action ever
# exists on a Mac.
if MACOS:
    ACTIONS = {"t3-pair": ACTIONS["t3-pair"]} if MACOS_AGENTS else {}
elif PORTABLE:
    # A portable workspace has no authority over its Docker host. Pairing and
    # tmux session endpoints stay useful; every host-mutating grave action is
    # absent at the server boundary, not merely hidden by the PWA.
    ACTIONS = {"t3-pair": ACTIONS["t3-pair"]}
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
SERVICE_WORKER = r"""const CACHE='gravedecay-shell-@OFFLINE@';
const OFFLINE=new URL('offline.html',self.location.href).href;
self.addEventListener('install',event=>{
  event.waitUntil(caches.open(CACHE).then(cache=>cache.add(new Request(OFFLINE,{cache:'reload'}))).then(()=>self.skipWaiting()));
});
self.addEventListener('activate',event=>{
  event.waitUntil(caches.keys().then(keys=>Promise.all(
    keys.filter(key=>key!==CACHE).map(key=>caches.delete(key)))).then(()=>self.clients.claim()));
});
self.addEventListener('fetch',event=>{
  if(event.request.mode!=='navigate')return;
  event.respondWith(fetch(event.request).catch(()=>caches.match(OFFLINE)));
});
self.addEventListener('push',event=>{
  let d={};
  try{d=event.data?event.data.json():{};}
  catch(e){d={body:event.data&&event.data.text()};}
  event.waitUntil(self.registration.showNotification(d.title||'gravedecay',{
    body:d.body||'',icon:'icon-192.png',badge:'icon-192.png',
    tag:d.tag||'gravedecay',data:{url:d.url||'./'}}));
});
self.addEventListener('notificationclick',event=>{
  event.notification.close();
  const url=new URL((event.notification.data&&event.notification.data.url)||'./',self.location.href).href;
  event.waitUntil(clients.matchAll({type:'window',includeUncontrolled:true}).then(list=>{
    for(const c of list){if(c.url===url&&'focus'in c)return c.focus();}
    return clients.openWindow(url);
  }));
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


def static_asset_path(name):
    """Resolve a PWA shell file from the source tree or the installed copy."""
    here = os.path.dirname(os.path.abspath(__file__))
    for directory in (os.path.join(here, "static"),
                      os.path.join(here, "dashboard-static")):
        path = os.path.join(directory, name)
        if os.path.isfile(path):
            return path
    return None


def static_asset(name, fallback):
    """Read source-tree or installed dashboard assets, with an embedded
    fallback so an interrupted/older upgrade never makes the UI unbootable."""
    path = static_asset_path(name)
    if path:
        with open(path, encoding="utf-8") as f:
            return f.read()
    return fallback


def static_asset_sha(name):
    """sha256 of the on-disk asset bytes — same digest `sha256sum` prints.

    Captured at import as SHELL_ID so /healthz reports what this process
    loaded, not whatever is on disk now. None if the file is missing
    (fallback is being served); that is also a doctor fail."""
    path = static_asset_path(name)
    if not path:
        return None
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


# Tiny last-resort page if raise copied the Python without index.html.
# Doctor's shell-hash check fails in that state; this only keeps HTTP up.
MISSING_SHELL = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>gravedecay · shell missing</title>
<style>body{margin:2rem;background:#070907;color:#a8e6a3;font:15px/1.6 ui-monospace,Menlo,monospace}</style>
</head><body><h1>🪦 dashboard shell missing</h1>
<p>index.html was not installed next to the dashboard. Re-run raise.sh (or macos/install.sh).</p>
</body></html>"""


def load_page():
    return static_asset("index.html", MISSING_SHELL).replace(
        "@HOST@", HOST).replace("@BASE@", BASE or "/grave")


PAGE = load_page()
# Import-time digest of the file we loaded, not a live re-read. Same contract
# as BUILD_ID: after raise replaces the file, a process that was not
# restarted still reports the old hash and doctor fails.
SHELL_ID = static_asset_sha("index.html")


def load_service_worker():
    """Stamp the worker's cache name with a digest of the offline page it
    pre-caches. sw.js is otherwise byte-stable, and browsers only re-install
    a worker whose bytes changed — without the stamp an upgrade that touched
    offline.html would leave the old copy in CacheStorage forever."""
    offline = static_asset("offline.html", OFFLINE_PAGE)
    stamp = hashlib.sha256(offline.encode()).hexdigest()[:12]
    return static_asset("sw.js", SERVICE_WORKER).replace("@OFFLINE@", stamp), stamp


SW, SW_ID = load_service_worker()


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


def launchagent_state(label):
    rc, out, _ = sh(["launchctl", "print", f"gui/{os.getuid()}/{label}"], timeout=3)
    return {"unit": label, "active": "active" if rc == 0 else "inactive",
            "sub": "launchd" if rc == 0 else "not loaded"}


def collect_services():
    if MACOS:
        return [launchagent_state("io.gravedecay.dashboard"),
                launchagent_state("io.gravedecay.network")]
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


MACOS_REPO_SCAN_MAX_DEPTH = 4
MACOS_REPO_SCAN_MAX_DIRS = 400
MACOS_REPO_SCAN_MAX_REPOS = 40
MACOS_GITHUB_REPO_LIMIT = 12
MACOS_GITHUB_ITEMS_PER_REPO = 3
MACOS_GITHUB_WORKERS = 4


def github_remote(value):
    """Return an owner/repository slug for a normal github.com remote only."""
    value = value.strip()
    m = re.search(r"github\.com[:/]([^/\s]+)/([^/\s]+?)(?:\.git)?/?$", value)
    if not m:
        return None
    slug = f"{m.group(1)}/{m.group(2)}"
    return slug if re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", slug) else None


def collect_macos_repo_inventory():
    """Discover a bounded set of nested local Git repositories under ~/Sites.

    No repo-provided string becomes a command: every git invocation is an argv
    list and the walker never follows symlinked directories.  A typical
    ``~/Sites/owner/repo`` layout is covered while an accidentally broad root
    cannot turn a dashboard refresh into an unbounded filesystem crawl.
    """
    configured = load_settings().get("repo_root", MACOS_REPO_ROOT_DEFAULT)
    root, error = macos_repo_root(configured)
    if error:
        return {"root": os.path.realpath(str(configured)), "repos": [], "error": error}
    cache_key = f"macos-repo-inventory:{root}"

    def fetch():
        if not shutil.which("git"):
            return {"root": root, "repos": [],
                    "error": "git is not installed or unavailable in the LaunchAgent PATH"}
        repos, seen_dirs = [], 0
        try:
            walker = os.walk(root, topdown=True, followlinks=False)
            for directory, dirs, _files in walker:
                # Do not enter hidden/tooling trees or symlinked directories;
                # a repo's own .git directory is never part of the scan.
                dirs[:] = sorted(d for d in dirs if d != ".git" and not os.path.islink(os.path.join(directory, d)))
                relative = os.path.relpath(directory, root)
                depth = 0 if relative == "." else relative.count(os.sep) + 1
                if depth > MACOS_REPO_SCAN_MAX_DEPTH:
                    dirs[:] = []
                    continue
                seen_dirs += 1
                if seen_dirs > MACOS_REPO_SCAN_MAX_DIRS:
                    break
                git_marker = os.path.join(directory, ".git")
                if not os.path.exists(git_marker):
                    continue
                rc, inside, _ = sh(["git", "-C", directory, "rev-parse", "--is-inside-work-tree"])
                if rc != 0 or inside.strip() != "true":
                    dirs[:] = []
                    continue
                _, branch, _ = sh(["git", "-C", directory, "rev-parse", "--abbrev-ref", "HEAD"])
                _, porcelain, _ = sh(["git", "-C", directory, "status", "--porcelain"])
                _, last, _ = sh(["git", "-C", directory, "log", "-1", "--format=%cr\t%s"])
                _, remote, _ = sh(["git", "-C", directory, "remote", "get-url", "origin"])
                when, _, subject = last.strip().partition("\t")
                repos.append({"name": relative if relative != "." else os.path.basename(root),
                              "branch": branch.strip() or "detached",
                              "dirty": len(porcelain.splitlines()),
                              "last_when": when, "last_subject": subject[:80],
                              "github_repo": github_remote(remote)})
                dirs[:] = []  # a nested repository is represented once
                if len(repos) >= MACOS_REPO_SCAN_MAX_REPOS:
                    break
        except OSError as e:
            return {"root": root, "repos": repos, "error": f"Repository scan failed: {e}"}
        truncated = seen_dirs > MACOS_REPO_SCAN_MAX_DIRS or len(repos) >= MACOS_REPO_SCAN_MAX_REPOS
        suffix = "repository scan limit reached" if truncated else None
        return {"root": root, "repos": sorted(repos, key=lambda repo: repo["name"]),
                "error": None, "warning": suffix, "truncated": truncated}
    return cached(cache_key, 30, fetch)


def collect_macos_work(inventory):
    """Read bounded GitHub work + CI concurrently for the local repo set."""
    root = inventory.get("root", "")
    cache_key = f"macos-work:{root}"

    def fetch():
        if inventory.get("error") and not inventory.get("repos"):
            return {"github": {"login": None, "repos": [], "error": inventory["error"]},
                    "ci": {"rows": [], "error": inventory["error"]}}
        if not shutil.which("gh"):
            error = "gh is not installed — install GitHub CLI, then run gh auth login"
            return {"github": {"login": None, "repos": [], "error": error},
                    "ci": {"rows": [], "error": error}}
        rc, out, _ = sh(["gh", "api", "user", "--jq", ".login"], timeout=10)
        login = out.strip() if rc == 0 and out.strip() else None
        if not login:
            error = "gh not authenticated — run gh auth login in Terminal"
            return {"github": {"login": None, "repos": [], "error": error},
                    "ci": {"rows": [], "error": error}}
        eligible_repos = sorted((repo for repo in inventory.get("repos", []) if repo.get("github_repo")),
                                key=lambda repo: repo["name"])
        capped = len(eligible_repos) > MACOS_GITHUB_REPO_LIMIT
        github_repos = eligible_repos[:MACOS_GITHUB_REPO_LIMIT]
        if not github_repos:
            error = "No GitHub origin remotes in the configured repository root"
            return {"github": {"login": login, "repos": [], "error": error},
                    "ci": {"rows": [], "error": error}}

        def api(repo, kind):
            slug = repo["github_repo"]
            if kind == "ci":
                return kind, repo, sh(["gh", "run", "list", "-R", slug, "-L", "1",
                                      "--json", "workflowName,conclusion,status,url,headBranch"], timeout=15)
            if kind == "issues":
                # REST /issues also includes pull requests; use gh's issue
                # endpoint so the bounded limit applies to actual issues.
                return kind, repo, sh(["gh", "issue", "list", "-R", slug, "--state", "open",
                                      "--limit", str(MACOS_GITHUB_ITEMS_PER_REPO),
                                      "--json", "number,title,url"], timeout=15)
            return kind, repo, sh(["gh", "api", "-X", "GET",
                                  f"repos/{slug}/pulls?state=open&per_page={MACOS_GITHUB_ITEMS_PER_REPO}"], timeout=15)

        results = {}
        with concurrent.futures.ThreadPoolExecutor(max_workers=MACOS_GITHUB_WORKERS) as pool:
            pending = [pool.submit(api, repo, kind) for repo in github_repos for kind in ("prs", "issues", "ci")]
            for future in concurrent.futures.as_completed(pending):
                kind, repo, result = future.result()
                results[(repo["name"], kind)] = result

        groups, ci_rows, errors = [], [], []
        for repo in github_repos:
            label, slug = repo["name"], repo["github_repo"]
            group = {"repo": label, "url": f"https://github.com/{slug}", "prs": [], "issues": []}
            for kind in ("prs", "issues"):
                rc, out, _ = results.get((label, kind), (1, "", ""))
                if rc != 0:
                    errors.append(label)
                    continue
                try:
                    rows = json.loads(out)
                except ValueError:
                    errors.append(label)
                    continue
                for row in rows[:MACOS_GITHUB_ITEMS_PER_REPO]:
                    group[kind].append({"number": row.get("number"),
                                        "title": str(row.get("title", ""))[:80],
                                        "url": row.get("url") or row.get("html_url", "")})
            rc, out, _ = results.get((label, "ci"), (1, "", ""))
            if rc == 0:
                try:
                    runs = json.loads(out)
                except ValueError:
                    runs = []
                if runs:
                    run = runs[0]
                    ci_rows.append({"repo": label, "workflow": run.get("workflowName", ""),
                                    "branch": run.get("headBranch", ""), "status": run.get("status", ""),
                                    "conclusion": run.get("conclusion") or "", "url": run.get("url", "")})
            else:
                errors.append(label)
            if group["prs"] or group["issues"]:
                groups.append(group)
        warnings = []
        if capped:
            warnings.append(f"remote work limited to first {MACOS_GITHUB_REPO_LIMIT} GitHub repositories")
        if errors:
            warnings.append("GitHub data unavailable for " + ", ".join(sorted(set(errors))[:3]))
        warning = "; ".join(warnings) or None
        return {"github": {"login": login, "repos": groups, "error": None, "warning": warning},
                "ci": {"rows": ci_rows, "error": None, "warning": warning}}
    return cached(cache_key, 120, fetch)


def collect_macos_github(inventory):
    return collect_macos_work(inventory)["github"]


def collect_macos_ci(inventory):
    return collect_macos_work(inventory)["ci"]


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


def _read_proc_stat():
    """{"cpu": (idle_ticks, total_ticks), "cpu0": …} — the aggregate plus one
    entry per logical cpu. Ticks are monotonic counters since boot."""
    stats = {}
    with open("/proc/stat") as f:
        for line in f:
            if not line.startswith("cpu"):
                break            # cpu lines come first; intr/ctxt/… follow
            parts = line.split()
            vals = [int(x) for x in parts[1:]]
            if len(vals) < 4:
                continue
            idle = vals[3] + (vals[4] if len(vals) > 4 else 0)  # idle + iowait
            stats[parts[0]] = (idle, sum(vals))
    return stats


# Utilisation is a rate, so it only exists between two samples: keep the
# previous /proc/stat snapshot module-wide and diff against it. Primed at
# import so the first poll already reports something.
try:
    _cpu_prev = _read_proc_stat()
except OSError:
    _cpu_prev = {}
_cpu_prev_t = time.monotonic()
_cpu_last = {"pct": None, "cores": []}

CPU_MIN_WINDOW = 1.0   # s


def collect_cpu():
    """Busy percentage for the whole package and for every logical cpu, over
    the window since the last call (the dashboard polls every 5 s). Two
    clients polling at once would otherwise race to a ~0 ms window and read
    pure noise, so anything inside CPU_MIN_WINDOW replays the last reading."""
    global _cpu_prev, _cpu_prev_t, _cpu_last
    now = time.monotonic()
    # A zero timestamp is the deliberate "sample now" sentinel used during
    # startup/tests; do not mistake an early process monotonic clock for a
    # sub-window poll.
    if _cpu_prev_t and now - _cpu_prev_t < CPU_MIN_WINDOW:
        return _cpu_last
    try:
        cur = _read_proc_stat()
    except OSError:
        return _cpu_last
    prev, _cpu_prev, _cpu_prev_t = _cpu_prev, cur, now

    def busy(key):
        if key not in prev or key not in cur:
            return None
        d_idle = cur[key][0] - prev[key][0]
        d_total = cur[key][1] - prev[key][1]
        if d_total <= 0:
            return None
        return round(min(100.0, max(0.0, (1 - d_idle / d_total) * 100)), 1)

    # sorted by cpu index, not by string, so cpu10 doesn't land next to cpu1
    keys = sorted((k for k in cur if k != "cpu" and k[3:].isdigit()),
                  key=lambda k: int(k[3:]))
    cores = [busy(k) for k in keys]
    _cpu_last = {"pct": busy("cpu"), "cores": [c for c in cores if c is not None]}
    return _cpu_last


def parse_macos_top_cpu(output):
    """Parse the one-line CPU summary emitted by ``top -l 1 -n 0 -F -R``."""
    m = re.search(r"CPU usage:\s*([\d.]+)%\s*user,\s*([\d.]+)%\s*sys,\s*([\d.]+)%\s*idle", output, re.I)
    if not m:
        return None
    try:
        return round(max(0.0, min(100.0, float(m.group(1)) + float(m.group(2)))), 1)
    except ValueError:
        return None


def parse_macos_memory_pressure(output):
    m = re.search(r"System-wide memory free percentage:\s*(\d+(?:\.\d+)?)%", output, re.I)
    if not m:
        return None
    try:
        return round(max(0.0, min(100.0, 100.0 - float(m.group(1)))), 1)
    except ValueError:
        return None


def parse_macos_vm_stat(output, page_size=None):
    """Return vm_stat pages and its reported page size, if either is usable."""
    pages = {}
    for name, value in re.findall(r"Pages ([^:]+):\s*(\d+)", output):
        try:
            pages[name.strip().lower()] = int(value)
        except ValueError:
            pass
    m = re.search(r"page size of\s*(\d+)\s*bytes", output, re.I)
    if m:
        page_size = int(m.group(1))
    return pages, page_size


def parse_macos_pmset_therm(output):
    values = {}
    for key, value in re.findall(r"(CPU_(?:Scheduler_Limit|Available_CPUs|Speed_Limit))\s*=\s*(\d+)", output):
        values[key] = int(value)
    limits = [values[k] for k in ("CPU_Scheduler_Limit", "CPU_Speed_Limit") if k in values]
    if not limits:
        return {"state": None, "speed_limit": None, "scheduler_limit": None, "available_cpus": None}
    return {"state": "throttled" if any(x < 100 for x in limits) else "nominal",
            "speed_limit": values.get("CPU_Speed_Limit"),
            "scheduler_limit": values.get("CPU_Scheduler_Limit"),
            "available_cpus": values.get("CPU_Available_CPUs")}


def parse_macos_pmset_batt(output):
    source = re.search(r"Now drawing from '([^']+)'", output, re.I)
    battery = re.search(r"\b(\d{1,3})%;\s*([^;\n]+)", output)
    if not battery:
        return None
    try:
        pct = max(0, min(100, int(battery.group(1))))
    except ValueError:
        return None
    return {"pct": pct, "state": battery.group(2).strip(),
            "power_source": source.group(1) if source else None}


def parse_macos_swapusage(output):
    values = {}
    for key, value in re.findall(r"\b(total|used|free)\s*=\s*([\d.]+)M", output, re.I):
        values[key.lower()] = float(value)
    if "total" not in values or "used" not in values:
        return {"total_mb": None, "used_mb": None, "free_mb": None}
    return {"total_mb": values["total"], "used_mb": values["used"],
            "free_mb": values.get("free")}


def _collect_macos_system():
    """Native, unprivileged Mac vitals. Kept together for one short TTL cache."""
    load = [0.0, 0.0, 0.0]
    rc, out, _ = sh(["uptime"])
    if rc == 0:
        m = re.search(r"load averages?:\s*([\d.]+)[, ]+\s*([\d.]+)[, ]+\s*([\d.]+)", out)
        if m:
            load = [float(x) for x in m.groups()]
    rc, out, _ = sh(["sysctl", "-n", "kern.boottime"])
    m = re.search(r"sec\s*=\s*(\d+)", out)
    uptime = int(time.time()) - int(m.group(1)) if m else 0

    rc, top_out, _ = sh(["top", "-l", "1", "-n", "0", "-F", "-R", "-stats", "cpu"], timeout=3)
    cpu_pct = parse_macos_top_cpu(top_out) if rc == 0 else None
    rc, pressure_out, _ = sh(["memory_pressure", "-Q"], timeout=3)
    pressure_pct = parse_macos_memory_pressure(pressure_out) if rc == 0 else None
    rc, vm_out, _ = sh(["vm_stat"])
    rc2, page_out, _ = sh(["sysctl", "-n", "hw.pagesize"])
    page_size = int(page_out.strip()) if rc2 == 0 and page_out.strip().isdigit() else None
    pages, page_size = parse_macos_vm_stat(vm_out if rc == 0 else "", page_size)
    rc, out, _ = sh(["sysctl", "-n", "hw.memsize"])
    total = int(out.strip()) if rc == 0 and out.strip().isdigit() else None
    compressed = pages.get("occupied by compressor", 0) * (page_size or 0)
    rc, therm_out, _ = sh(["pmset", "-g", "therm"])
    thermal = parse_macos_pmset_therm(therm_out) if rc == 0 else parse_macos_pmset_therm("")
    rc, batt_out, _ = sh(["pmset", "-g", "batt"])
    battery = parse_macos_pmset_batt(batt_out) if rc == 0 else None
    rc, swap_out, _ = sh(["sysctl", "-n", "vm.swapusage"])
    swap = parse_macos_swapusage(swap_out) if rc == 0 else parse_macos_swapusage("")
    u = shutil.disk_usage("/")
    return {"load": load, "ncpu": os.cpu_count(), "cpu": {"pct": cpu_pct, "cores": []},
            # This is reclaimability pressure, not physical RAM occupancy.
            "mem": {"total_kb": (total or 0) // 1024, "used_kb": 0,
                    "pct": pressure_pct, "pressure_pct": pressure_pct,
                    "compressed_kb": compressed // 1024},
            "disks": [{"label": "/", "total": u.total, "used": u.used,
                       "pct": round(u.used / u.total * 100, 1)}],
            "uptime_s": uptime, "temps": {"cpu": None, "gpu": None,
            "gpu_mhz": None, "gpu_state": None, "fans": []},
            "thermal": thermal, "battery": battery, "swap": swap}


def collect_system():
    if MACOS:
        # top costs about half a second on Intel Macs. Dashboard polling may be
        # as fast as 2 seconds, so share one native sample for five seconds.
        return cached("macos-system", 5, _collect_macos_system)
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
        "ncpu": os.cpu_count(), "cpu": collect_cpu(),
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


def _summary_links():
    """Public, origin-relative destinations.  Keep this list deliberately
    boring: it is consumed by native clients and must not expose configured
    app tiles or user-supplied URLs."""
    links = {"dashboard": (BASE or "") + "/"}
    if not PORTABLE and (not MACOS or any(a.get("url") == "/net/" for a in APPS)):
        links["network"] = "/net/"
    if not MACOS or MACOS_AGENTS:
        links.update({"t3": "/", "terminal": "/term/"})
    return links


def _summary():
    """Small local-only status contract for thin clients.

    Do not route this through state(): state intentionally includes expensive
    collectors and owner-facing detail.  This calls only local, bounded
    collectors and omits names, logs, paths, and container/session contents.
    """
    if PORTABLE:
        tmux = collect_tmux()
        return {
            "product": "gravedecay", "api_version": 1,
            "observed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "node": {"host": HOST, "platform": "container", "mode": "developer", "uptime_s": None},
            "resources": {"cpu_pct": None, "memory_pct": None, "disk_pct": None,
                          "cpu_temp_c": None, "gpu_temp_c": None},
            "activity": {"sessions_live": len(tmux), "sessions_frozen": 0},
            "health": {"services_failed": 0, "containers_problem": 0},
            "links": _summary_links(),
        }

    system = collect_system()
    mode = "developer"
    frozen = False
    tmux = []
    services = collect_services()
    containers_problem = 0
    if MACOS:
        if MACOS_AGENTS:
            tmux = collect_tmux()
    else:
        mode = "developer" if unit_state("t3code").get("active") == "active" else "gaming"
        tmux = collect_tmux()
        try:
            with open("/sys/fs/cgroup/grave-torpor/cgroup.freeze") as f:
                frozen = f.read().strip() == "1"
        except OSError:
            pass
        # Gaming deliberately stops developer services and Docker.  Only a
        # genuine systemd failure is a problem in that mode.
        if mode != "gaming":
            docker = collect_docker()
            containers_problem = sum(1 for row in docker["containers"]
                                     if row.get("state") != "running"
                                     or "(unhealthy)" in row.get("status", ""))
    disks = system.get("disks") or []
    temps = system.get("temps") or {}
    return {
        "product": "gravedecay", "api_version": 1,
        "observed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "node": {"host": HOST, "platform": "macos" if MACOS else "linux",
                 "mode": mode, "uptime_s": system.get("uptime_s")},
        "resources": {"cpu_pct": (system.get("cpu") or {}).get("pct"),
                      "memory_pct": (system.get("mem") or {}).get("pct"),
                      "disk_pct": disks[0].get("pct") if disks else None,
                      "cpu_temp_c": temps.get("cpu"), "gpu_temp_c": temps.get("gpu")},
        "activity": {"sessions_live": 0 if frozen else len(tmux),
                     "sessions_frozen": len(tmux) if frozen else 0},
        "health": {"services_failed": sum(1 for row in services if row.get("active") == "failed"),
                   "containers_problem": containers_problem},
        "links": _summary_links(),
    }


def summary():
    return cached("summary", 5, _summary)


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


# ---------- notifications: ntfy channel + Web Push (docs/NOTIFICATIONS.md) ----------
# grave notify owns delivery to ntfy; the dashboard owns Web Push to enrolled
# PWA devices (it holds the VAPID key and the subscription store) and exposes
# /api/push-send on loopback so grave's push leg is one curl away.
NOTIFY_ENV = os.path.join(GRAVE_ROOT, "config", "secrets", "notify.env")
# Event-class override written by the ⚙️ checkboxes. grave prefers this file
# over grave.conf's NOTIFY_EVENTS because the dashboard runs unprivileged and
# must not need sudo to flip a notification preference (same reasoning as the
# gamewatch flag file).
NOTIFY_EVENTS_PATH = os.path.join(GRAVE_ROOT, "config", "notify-events")
NOTIFY_CLASSES = ["session-exit", "bell", "agent-done", "unit-failure", "doctor",
                  "digest"]
GRAVE_CONF = os.environ.get("GRAVE_CONF", "/etc/gravedecay/grave.conf")
VAPID_PEM = os.path.join(GRAVE_ROOT, "config", "secrets", "vapid.pem")
PUSH_SUBS_PATH = os.path.join(GRAVE_ROOT, "config", "push-subscriptions.json")
PUSH_SUBS_MAX = 10
_PUSH_LOCK = threading.Lock()


@functools.cache
def _webpush_crypto():
    """cryptography primitives for Web Push (VAPID ES256 + RFC 8291 aes128gcm).
    Optional exactly like PIL: without python3-cryptography the dashboard runs
    fine and Web Push reports itself unsupported (ntfy is unaffected).
    Hand-rolling EC/AES-GCM in stdlib is the one thing we refuse to do here."""
    try:
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        from cryptography.hazmat.primitives.kdf.hkdf import HKDF
        return {"hashes": hashes, "serialization": serialization, "ec": ec,
                "decode_dss": decode_dss_signature, "AESGCM": AESGCM, "HKDF": HKDF}
    except Exception:
        return None


def _b64u(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _b64u_dec(s):
    s = str(s).strip()
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def vapid_private_key():
    """Load-or-create the appliance's VAPID signing key (P-256, PEM, 600 in the
    secret store). One key per box: rotating it orphans every subscription."""
    c = _webpush_crypto()
    if not c:
        return None
    ser, ec = c["serialization"], c["ec"]
    try:
        with open(VAPID_PEM, "rb") as f:
            return ser.load_pem_private_key(f.read(), password=None)
    except (OSError, ValueError):
        pass
    key = ec.generate_private_key(ec.SECP256R1())
    pem = key.private_bytes(ser.Encoding.PEM, ser.PrivateFormat.PKCS8, ser.NoEncryption())
    os.makedirs(os.path.dirname(VAPID_PEM), mode=0o700, exist_ok=True)
    fd = os.open(VAPID_PEM, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(pem)
    return key


def vapid_public_b64():
    key = vapid_private_key()
    if not key:
        return None
    ser = _webpush_crypto()["serialization"]
    raw = key.public_key().public_bytes(ser.Encoding.X962, ser.PublicFormat.UncompressedPoint)
    return _b64u(raw)


def _load_push_subs():
    try:
        with open(PUSH_SUBS_PATH) as f:
            subs = json.load(f).get("subscriptions", [])
        return [s for s in subs if isinstance(s, dict) and s.get("endpoint")]
    except (OSError, ValueError):
        return []


def _save_push_subs(subs):
    tmp = PUSH_SUBS_PATH + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump({"subscriptions": subs}, f, indent=2)
    os.replace(tmp, PUSH_SUBS_PATH)


def _sub_id(endpoint):
    """Stable opaque device id. The endpoint itself is a push-service
    capability URL — it never leaves the box (the UI sees only this hash)."""
    return hashlib.sha256(endpoint.encode()).hexdigest()[:12]


def push_subscribe(data):
    sub = data.get("subscription") or {}
    endpoint = str(sub.get("endpoint", ""))
    keys = sub.get("keys") or {}
    if not endpoint.startswith("https://") or len(endpoint) > 1024:
        return {"ok": False, "output": "bad endpoint"}
    try:
        p256dh = _b64u_dec(keys.get("p256dh", ""))
        auth = _b64u_dec(keys.get("auth", ""))
    except (ValueError, binascii.Error):
        return {"ok": False, "output": "bad subscription keys"}
    if len(p256dh) != 65 or p256dh[:1] != b"\x04" or len(auth) != 16:
        return {"ok": False, "output": "bad subscription keys"}
    label = re.sub(r"[^\w .,/()\-]", "", str(data.get("label", "")))[:40] or "device"
    entry = {"endpoint": endpoint, "p256dh": _b64u(p256dh), "auth": _b64u(auth),
             "label": label, "added": time.strftime("%Y-%m-%d %H:%M")}
    with _PUSH_LOCK:
        subs = [s for s in _load_push_subs() if s["endpoint"] != endpoint]
        subs.append(entry)
        subs = subs[-PUSH_SUBS_MAX:]
        _save_push_subs(subs)
    return {"ok": True, "id": _sub_id(endpoint), "devices": len(subs)}


def push_unsubscribe(data):
    endpoint, sid = str(data.get("endpoint", "")), str(data.get("id", ""))
    with _PUSH_LOCK:
        subs = _load_push_subs()
        kept = [s for s in subs
                if s["endpoint"] != endpoint and _sub_id(s["endpoint"]) != sid]
        _save_push_subs(kept)
    return {"ok": True, "removed": len(subs) - len(kept)}


def _webpush_encrypt(p256dh, auth, payload, _salt=None, _eph=None):
    """RFC 8291 aes128gcm message encryption (single record, minimal padding).
    _salt/_eph exist ONLY so the test suite can pin the RFC's Appendix A
    vector; production callers never pass them."""
    c = _webpush_crypto()
    ec, hashes, HKDF, AESGCM = c["ec"], c["hashes"], c["HKDF"], c["AESGCM"]
    ser = c["serialization"]
    ua_pub = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), p256dh)
    eph = _eph or ec.generate_private_key(ec.SECP256R1())
    as_pub = eph.public_key().public_bytes(ser.Encoding.X962, ser.PublicFormat.UncompressedPoint)
    shared = eph.exchange(ec.ECDH(), ua_pub)
    prk = HKDF(algorithm=hashes.SHA256(), length=32, salt=auth,
               info=b"WebPush: info\x00" + p256dh + as_pub).derive(shared)
    salt = _salt or os.urandom(16)
    cek = HKDF(algorithm=hashes.SHA256(), length=16, salt=salt,
               info=b"Content-Encoding: aes128gcm\x00").derive(prk)
    nonce = HKDF(algorithm=hashes.SHA256(), length=12, salt=salt,
                 info=b"Content-Encoding: nonce\x00").derive(prk)
    body = AESGCM(cek).encrypt(nonce, payload + b"\x02", None)
    header = salt + (4096).to_bytes(4, "big") + bytes([len(as_pub)]) + as_pub
    return header + body


# RFC 8292 contact claim. MUST be a real-shaped contact: Apple validates it
# and answers 403 BadJwtToken for e.g. a hostname-derived mailto with no TLD
# (#97) — Chrome/Firefox don't care, so only iOS devices went dark. Both this
# URL and a syntactically valid mailto are verified accepted by Apple.
VAPID_SUB = "https://github.com/projectmushroom/gravedecay"


def _vapid_auth(endpoint):
    """RFC 8292 Authorization header: ES256 JWT audienced to the push service."""
    c = _webpush_crypto()
    key = vapid_private_key()
    origin = urllib.parse.urlparse(endpoint)
    claims = {"aud": f"{origin.scheme}://{origin.netloc}",
              "exp": int(time.time()) + 12 * 3600,
              "sub": VAPID_SUB}
    head = _b64u(json.dumps({"typ": "JWT", "alg": "ES256"}).encode())
    body = _b64u(json.dumps(claims).encode())
    der = key.sign(f"{head}.{body}".encode(), c["ec"].ECDSA(c["hashes"].SHA256()))
    r, s = c["decode_dss"](der)
    sig = _b64u(r.to_bytes(32, "big") + s.to_bytes(32, "big"))
    return f"vapid t={head}.{body}.{sig}, k={vapid_public_b64()}"


PUSH_URGENCY = {"min": "very-low", "low": "low", "default": "normal",
                "high": "high", "urgent": "high"}


def push_send(data):
    """Deliver one notification to every enrolled device. Callers: grave
    notify's push leg (loopback curl) and the ⚙️ test button. 404/410 from the
    push service means the subscription is dead — prune it so the store
    self-heals when a device revokes permission or reinstalls the PWA."""
    if not _webpush_crypto():
        return {"ok": False, "output": "python3-cryptography missing — Web Push unavailable"}
    with _PUSH_LOCK:
        subs = _load_push_subs()
    if not subs:
        return {"ok": False, "output": "no devices enrolled"}
    title = str(data.get("title", ""))[:120] or "gravedecay"
    body = str(data.get("body", ""))[:500]
    url = str(data.get("url", ""))
    if not url.startswith("/") or url.startswith("//"):
        url = ""   # deep links must stay on this origin; anything else is dropped
    tag = re.sub(r"[^\w-]", "", str(data.get("tag", "")))[:40] or "gravedecay"
    payload = json.dumps({"title": title, "body": body,
                          "url": url or None, "tag": tag}).encode()
    urgency = PUSH_URGENCY.get(str(data.get("priority", "default")), "normal")
    sent, gone, errors = 0, [], []
    for sub in subs:
        try:
            message = _webpush_encrypt(_b64u_dec(sub["p256dh"]), _b64u_dec(sub["auth"]), payload)
            req = urllib.request.Request(sub["endpoint"], data=message, method="POST", headers={
                "TTL": "86400", "Urgency": urgency,
                "Content-Encoding": "aes128gcm",
                "Content-Type": "application/octet-stream",
                "Authorization": _vapid_auth(sub["endpoint"]),
            })
            with urllib.request.urlopen(req, timeout=10):
                sent += 1
        except urllib.error.HTTPError as e:
            if e.code in (404, 410):
                gone.append(sub["endpoint"])
            else:
                # Keep the service's reason body: a bare "HTTP 403" hides the
                # difference between BadJwtToken, VapidPkHashMismatch, … (#97)
                try:
                    detail = " " + e.read(200).decode("utf-8", "replace").strip()
                except Exception:
                    detail = ""
                errors.append(f"{sub['label']}: HTTP {e.code}{detail}")
        except Exception as e:
            errors.append(f"{sub['label']}: {e}")
    if gone:
        with _PUSH_LOCK:
            _save_push_subs([s for s in _load_push_subs() if s["endpoint"] not in gone])
    summary = f"pushed to {sent}/{len(subs)} device(s)"
    if errors:
        summary += " — " + "; ".join(errors[:2])
    return {"ok": sent > 0, "sent": sent, "pruned": len(gone), "errors": errors[:5],
            "output": summary}


def _read_env_file(path):
    vals = {}
    try:
        with open(path) as f:
            for line in f:
                k, _, v = line.strip().partition("=")
                if k and v:
                    vals[k] = v
    except OSError:
        pass
    return vals


def ntfy_configured():
    return _read_env_file(NOTIFY_ENV).get("NTFY_URL", "").startswith("http")


def save_ntfy(url=None, token=None, clear=False):
    """Write the ntfy channel into the secret store. Empty fields keep the
    existing value (matching the UI's 'leave empty to keep' contract)."""
    if clear:
        try:
            os.remove(NOTIFY_ENV)
        except OSError:
            pass
        return
    vals = _read_env_file(NOTIFY_ENV)
    if url:
        if not re.fullmatch(r"https?://[^\s\"'`\\]+", url):
            raise ValueError("bad ntfy url")
        vals["NTFY_URL"] = url
    if token:
        if not re.fullmatch(r"[\w.\-]{1,200}", token):
            raise ValueError("bad ntfy token")
        vals["NTFY_TOKEN"] = token
    if not vals.get("NTFY_URL"):
        raise ValueError("ntfy url required")
    os.makedirs(os.path.dirname(NOTIFY_ENV), mode=0o700, exist_ok=True)
    fd = os.open(NOTIFY_ENV, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        for k in ("NTFY_URL", "NTFY_TOKEN"):
            if vals.get(k):
                f.write(f"{k}={vals[k]}\n")


def notify_events():
    """Enabled event classes: the ⚙️ override file wins, else grave.conf's
    NOTIFY_EVENTS, else all classes (grave defaults the same way)."""
    try:
        with open(NOTIFY_EVENTS_PATH) as f:
            return [w for w in f.read().split() if w in NOTIFY_CLASSES]
    except OSError:
        pass
    try:
        with open(GRAVE_CONF) as f:
            for line in f:
                m = re.match(r'NOTIFY_EVENTS="([^"]*)"', line.strip())
                if m:
                    return [w for w in m.group(1).split() if w in NOTIFY_CLASSES]
    except OSError:
        pass
    return list(NOTIFY_CLASSES)


def save_notify_events(words):
    if not isinstance(words, list):
        raise ValueError("bad events")
    words = [w for w in words if w in NOTIFY_CLASSES]
    tmp = NOTIFY_EVENTS_PATH + ".tmp"
    with open(tmp, "w") as f:
        f.write(" ".join(words) + "\n")
    os.replace(tmp, NOTIFY_EVENTS_PATH)


def notify_state():
    """Settings-panel view of the notification stack (owner-only in state())."""
    crypto = _webpush_crypto() is not None
    subs = _load_push_subs()
    return {
        "ntfy": ntfy_configured(),
        "events": notify_events(),
        "classes": NOTIFY_CLASSES,
        "push": {
            "supported": crypto,
            "reason": None if crypto else "python3-cryptography not installed on the box",
            "devices": [{"id": _sub_id(s["endpoint"]), "label": s["label"],
                         "added": s.get("added", "")} for s in subs],
        },
    }


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


def _rel(ts):
    d = max(0, int(time.time() - ts))
    if d < 90:
        return f"{d}s ago"
    if d < 5400:
        return f"{d // 60}m ago"
    if d < 172800:
        return f"{d // 3600}h ago"
    return f"{d // 86400}d ago"


def collect_inbox(limit=50):
    """Durable copy of delivered pages (#119), written by `grave notify` to
    logs/notifications.jsonl — pushes are ephemeral on every channel, this is
    where a dismissed or truncated one can still be read. Owner-only: bodies
    carry private detail (session names, doctor output)."""
    items = []
    try:
        with open(f"{GRAVE_ROOT}/logs/notifications.jsonl") as f:
            lines = f.readlines()[-limit:]
    except OSError:
        return items
    for line in lines:
        try:
            d = json.loads(line)
        except ValueError:
            continue
        items.append({"when": _rel(int(d.get("ts", 0))), "event": d.get("event", ""),
                      "title": d.get("title", ""), "body": d.get("body", ""),
                      "link": d.get("link") or "/grave/",
                      "delivered": bool(d.get("delivered"))})
    items.reverse()
    return items


def collect_agent_history(limit=20):
    """Past agent sessions (#110): the pipe-pane logs under agents/<name>/
    survive tmux death. agents/t3code is T3 server state, not a session dir;
    the name allowlist also keeps hostile dir names out of the UI and the
    /api/agent-log path join."""
    out = []
    base = f"{GRAVE_ROOT}/agents"
    try:
        names = os.listdir(base)
    except OSError:
        return out
    for name in names:
        d = os.path.join(base, name)
        if name == "t3code" or not re.fullmatch(r"[A-Za-z0-9_-]+", name) \
           or not os.path.isdir(d):
            continue
        logs = sorted(g for g in os.listdir(d)
                      if re.fullmatch(r"session-\d{8}\.log", g))
        if not logs:
            continue
        try:
            last = int(os.path.getmtime(os.path.join(d, logs[-1])))
            size = sum(os.path.getsize(os.path.join(d, g)) for g in logs)
        except OSError:
            continue
        out.append({"name": name, "last": last, "last_when": _rel(last),
                    "logs": logs[::-1], "size_kb": size // 1024})
    out.sort(key=lambda x: -x["last"])
    return out[:limit]


def boot_mode():
    rc, _, _ = sh(["systemctl", "is-enabled", "--quiet", "t3code"])
    return "developer" if rc == 0 else "gaming"


def gamewatch_state():
    """Game-mode auto-throttle: installed? on? watcher running? (Steam Machine)."""
    installed = sh(["systemctl", "cat", "gravedecay-gamewatch.service"])[0] == 0
    on = os.path.exists(os.path.join(GRAVE_ROOT, "config", "gamewatch.on"))
    running = sh(["systemctl", "is-active", "--quiet", "gravedecay-gamewatch"])[0] == 0
    return {"installed": installed, "on": on, "running": running}


def keepalive_state():
    """"Always alive" tailnet keepalive: installed? on? loop running?"""
    installed = sh(["systemctl", "cat", "gravedecay-keepalive.service"])[0] == 0
    on = os.path.exists(os.path.join(GRAVE_ROOT, "config", "keepalive.on"))
    running = sh(["systemctl", "is-active", "--quiet", "gravedecay-keepalive"])[0] == 0
    return {"installed": installed, "on": on, "running": running}


def t3_connect_state():
    """T3 Connect: the declared mode (config/t3-connect.mode, the operator's
    intent — read fresh) plus the T3 CLI's persisted link state for the
    appliance instance (a ~1 s node spawn, so cached). Doctor enforces the
    match; this only paints it in settings."""
    mode = "off"
    try:
        with open(os.path.join(GRAVE_ROOT, "config", "t3-connect.mode")) as f:
            m = f.read().strip()
        if m in ("publish", "full"):
            mode = m
    except OSError:
        pass

    def fetch():
        rc, out, _ = sh([T3, "connect", "status", "--json",
                         "--base-dir", T3_BASE_DIR], timeout=20)
        if rc != 0:
            return None
        try:
            return json.loads(out)
        except ValueError:
            return None
    st = cached("t3connect", 20, fetch)
    return {"mode": mode, "ok": st is not None,
            "desired": bool(st and st.get("desired")),
            "linked": bool(st and st.get("linked")),
            "publish": bool(st and st.get("publishAgentActivity"))}


def state(headers):
    if MACOS:
        # No T3, terminal, Docker, privileged controls, or Linux data on Mac.
        # Unlike machine vitals, project names, commit subjects and integration
        # data are owner-private.  Localhost is trusted; a Serve viewer must
        # exactly match the installer-configured local Tailscale login.
        viewer = headers.get("Tailscale-User-Login")
        owner = viewer is None or viewer in ALLOWED_USERS
        settings = load_settings()
        if not owner:
            # A configured path is part of the local project topology too.
            # Do not hand it to a tailnet observer merely because preferences
            # are otherwise harmless to render.
            settings = dict(settings)
            settings["repo_root"] = ""
        private = {"github": {"login": None, "prs": [], "issues": [], "error": "restricted"},
                   "linear": {"configured": False, "issues": [], "error": "restricted"},
                   "ci": {"rows": [], "error": "restricted"}, "repos": [],
                   "repo_scan": {"root": None, "error": "restricted"}}
        if owner:
            inventory = collect_macos_repo_inventory()
            work = collect_macos_work(inventory)
            private = {"github": work["github"], "linear": collect_linear(), "ci": work["ci"],
                       "repos": inventory["repos"],
                       "repo_scan": {"root": inventory["root"], "error": inventory.get("error"),
                                     "warning": inventory.get("warning"),
                                     "truncated": inventory.get("truncated", False)}}
        return {"host": HOST, "now": time.strftime("%H:%M:%S"),
                "viewer": viewer or "local", "platform": "macos",
                "macos_agents": MACOS_AGENTS,
                "mode": "developer", "boot_mode": None, "gamewatch": None,
                "keepalive": None, "apps": list(APPS), "settings": settings,
                "github": private["github"], "linear": private["linear"], "ci": private["ci"],
                "usage": None, "notify": None, "services": collect_services(),
                "docker": {"error": "not managed on macOS", "containers": []},
                # Session names are owner-private like the rest of the work
                # plane; killing them is POST-gated separately.
                "tmux": collect_tmux() if MACOS_AGENTS and owner else [],
                "torpor": 0, "repos": private["repos"], "repo_scan": private["repo_scan"], "journal": [], "system": collect_system(),
                "backups": {"count": 0, "latest": None}, "inbox": [], "agent_history": []}
    if PORTABLE:
        # Deliberately work-plane only: do not read systemd, the host Docker
        # daemon, journald, cgroups, or host hardware from a container.
        viewer = headers.get("Tailscale-User-Login")
        restricted = viewer is not None and viewer not in ALLOWED_USERS
        private = {"github": {"login": None, "prs": [], "error": "restricted"},
                   "linear": {"configured": False, "issues": [], "error": "restricted"},
                   "ci": {"rows": [], "error": "restricted"}, "usage": None,
                   "repos": [], "inbox": [], "agent_history": []}
        if not restricted:
            gh = collect_github()
            private = {"github": gh, "linear": collect_linear(), "ci": collect_ci(),
                       "usage": collect_agent_usage(), "repos": collect_repos(),
                       "inbox": collect_inbox(), "agent_history": collect_agent_history()}
        return {"host": HOST, "now": time.strftime("%H:%M:%S"),
                "viewer": viewer or "local", "platform": "container", "mode": "developer",
                "boot_mode": None, "gamewatch": None, "keepalive": None,
                "apps": list(APPS), "settings": load_settings(), "notify": None,
                "github": private["github"], "linear": private["linear"], "ci": private["ci"],
                "usage": private["usage"], "services": [],
                "docker": {"error": "not managed by portable workspace", "containers": []},
                "tmux": collect_tmux(), "torpor": 0, "repos": private["repos"], "journal": [],
                "system": {"uptime_s": 0, "temps": {"cpu": None, "gpu": None,
                           "gpu_mhz": None, "gpu_state": None, "fans": []},
                           "cpu": {"pct": None, "cores": []}, "load": [0, 0, 0], "ncpu": 0,
                           "mem": {"pct": 0, "used_kb": 0, "total_kb": 0}, "disks": []},
                "backups": {"count": 0, "latest": None}, "inbox": private["inbox"],
                "agent_history": private["agent_history"]}
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
            "keepalive": keepalive_state(), "notify": notify_state(),
            "tmux": tmux, "torpor": len(tmux) if frozen else 0,
            "system": collect_system(),
            "github": {"login": None, "prs": [], "error": "paused in game mode"},
            "linear": {"configured": False, "issues": [], "error": None},
            "ci": {"rows": []}, "usage": None, "services": [], "repos": [],
            "docker": {"error": "docker stopped (gaming)", "containers": []},
            "journal": [], "backups": {"count": 0, "latest": None},
            "inbox": collect_inbox(), "agent_history": collect_agent_history(),
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
            "keepalive": keepalive_state(),
            "apps": list(APPS), "settings": load_settings(),
            "github": {"login": None, "prs": [], "error": "restricted"},
            "linear": {"configured": False, "issues": [], "error": None},
            "ci": {"rows": []}, "usage": None,
            "services": collect_services(), "docker": collect_docker(),
            "tmux": tmux, "torpor": len(tmux) if frozen else 0,
            "repos": [], "journal": [], "system": collect_system(),
            "backups": collect_backups(),
            # Delivered pages and session transcripts are owner-private, same
            # reasoning as the journal/repos withholding above.
            "inbox": [], "agent_history": [],
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
        "keepalive": keepalive_state(),
        "apps": apps,
        "github": gh,
        "ci": collect_ci(),
        "linear": collect_linear(),
        "usage": collect_agent_usage(),
        "settings": load_settings(),
        "notify": notify_state(),
        "t3_connect": t3_connect_state(),
        "services": collect_services(),
        "docker": collect_docker(),
        "tmux": tmux,
        "torpor": 0,
        "repos": collect_repos(),
        "journal": collect_journal(),
        "system": collect_system(),
        "backups": collect_backups(),
        "inbox": collect_inbox(),
        "agent_history": collect_agent_history(),
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

    def _backend_forbidden(self, path):
        if path == "/healthz":
            return False
        if REQUIRE_BACKEND_TOKEN and len(BACKEND_TOKEN) < 32:
            self._send(401, '{"error":"gateway capability required"}')
            return True
        if not BACKEND_TOKEN:
            return False
        if hmac.compare_digest(self.headers.get("X-Grave-Backend-Token", ""), BACKEND_TOKEN):
            return False
        self._send(401, '{"error":"gateway capability required"}')
        return True

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
                cmd = cmd + ["--base-url", f"{public_scheme(self.headers)}://{host}"]
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
        if self._backend_forbidden(p):
            return
        macos_gets = ("/", "/healthz", "/api/state", "/api/v1/summary", "/api/admin/releases", "/api/admin/update-status", "/manifest.webmanifest", "/sw.js", "/offline.html", "/apple-touch-icon.png", "/icon-180.png", "/icon-192.png", "/icon-512.png")
        # Agents layer: the pairing token streams over the same viewer- and
        # CSRF-gated SSE console as on Linux (_stream_action re-checks both).
        if MACOS_AGENTS:
            macos_gets += ("/api/action-stream",)
        if MACOS and p not in macos_gets:
            self._send(404, '{"error":"unavailable in macOS companion"}')
            return
        if p == "/api/action-stream":
            self._stream_action()
            return
        if p == "/healthz":
            # build = running Python; shell = on-disk index.html. Doctor
            # compares both to the installed files so a stale UI is visible
            # even after the page left this source file.
            self._send(200, json.dumps({
                "ok": True,
                "build": BUILD_ID,
                "shell": SHELL_ID,
                "sw": SW_ID,
            }))
        elif p == "/api/state":
            self._send(200, json.dumps(state(self.headers)))
        elif p == "/api/v1/summary":
            self._send(200, json.dumps(summary()))
        elif p == "/api/admin/releases":
            if PORTABLE:
                self._send(404, '{"error":"unavailable in portable workspace"}')
                return
            if self._forbidden():
                return
            rc, out, err = sh([MACOS_GRAVE if MACOS else GRAVE, "releases", "--json"], timeout=30)
            if rc:
                self._send(502, json.dumps({"ok": False, "output": ANSI.sub("", out + err)}))
            else:
                self._send(200, out)
        elif p == "/api/admin/update-status":
            if not MACOS: self._send(404, '{"error":"unavailable outside macOS companion"}'); return
            if self._forbidden(): return
            rc, out, err = sh([MACOS_GRAVE, "update-status"], timeout=10)
            self._send(200 if not rc else 502, out if not rc else json.dumps({"ok": False, "output": ANSI.sub("", out + err)}))
        elif p == "/api/agent-log":
            # Session transcript viewer (#110). Owner-gated like the file
            # manager — transcripts show everything an agent saw or did. Both
            # params land in a filesystem path, so allowlist their shape (the
            # same charsets `grave agents new` enforces and pipe-pane writes).
            if self._forbidden():
                return
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            name = q.get("name", [""])[0]
            fname = q.get("file", [""])[0]
            if not re.fullmatch(r"[A-Za-z0-9_-]{1,50}", name) \
               or not re.fullmatch(r"session-\d{8}\.log", fname):
                self._send(400, json.dumps({"ok": False, "output": "bad session or file name"}))
                return
            path = os.path.join(GRAVE_ROOT, "agents", name, fname)
            try:
                with open(path, "rb") as f:
                    f.seek(max(0, os.path.getsize(path) - 65536))
                    text = f.read().decode("utf-8", "replace")
            except OSError:
                self._send(404, json.dumps({"ok": False, "output": "no such log"}))
                return
            # Raw terminal capture: strip full CSI/OSC escape sequences (ANSI
            # only matches SGR color codes), then leftover control bytes.
            text = re.sub(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b\][^\x07\x1b]*(\x07|\x1b\\\\)?|\x1b.", "", text)
            text = re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", "", text)
            self._send(200, json.dumps({"ok": True, "name": name, "file": fname,
                                        "text": text}))
        elif p == "/api/push-key":
            # The VAPID public key a device needs to subscribe. Gated like the
            # file manager: only allowed viewers (and localhost) may enroll.
            if self._forbidden():
                return
            key = vapid_public_b64()
            if key:
                self._send(200, json.dumps({"ok": True, "key": key}))
            else:
                self._send(501, json.dumps({
                    "ok": False,
                    "output": "python3-cryptography not installed — Web Push unavailable"}))
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
            self._send(200, SW,
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
        if self._backend_forbidden(p):
            return
        macos_posts = ("/api/settings", "/api/admin/upgrade")
        if MACOS_AGENTS:
            # Deliberate reopening for the agents layer, still behind the
            # exact-LoginName ALLOWED_USERS gate just below: session kill and
            # scrollback copy for the tmux -L agents panel, and /api/action
            # (which on macOS only ever contains t3-pair).
            macos_posts += ("/api/action", "/api/session-kill", "/api/session-capture")
        if MACOS and p not in macos_posts:
            self._send(404, '{"error":"unavailable in macOS companion"}')
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
        if not isinstance(data, dict):
            self._send(400, json.dumps({"ok": False, "output": "JSON object required"}))
            return
        if p == "/api/fs":
            self._send(200, json.dumps(fs_op(data)))
            return
        if p == "/api/settings":
            try:
                if MACOS:
                    allowed = {"panel_order", "hidden_panels", "hidden_apps", "newtab_apps",
                               "modal_apps", "custom_apps", "poll_ms", "repo_root", "linear_key"}
                    if set(data) - allowed:
                        raise ValueError("macOS settings only accept UI preferences")
                    if "repo_root" in data:
                        root, error = macos_repo_root(data["repo_root"])
                        if error:
                            raise ValueError(error)
                        data["repo_root"] = root
                key = data.pop("linear_key", "")
                if isinstance(key, str) and key.strip():
                    save_linear_key(key)
                # Notification prefs ride the same save: ntfy channel into the
                # secret store, event classes into the grave-readable override.
                if data.pop("ntfy_clear", False):
                    save_ntfy(clear=True)
                else:
                    ntfy_url = str(data.pop("ntfy_url", "") or "").strip()
                    ntfy_token = str(data.pop("ntfy_token", "") or "").strip()
                    if ntfy_url or ntfy_token:
                        save_ntfy(ntfy_url or None, ntfy_token or None)
                events = data.pop("notify_events", None)
                if events is not None:
                    save_notify_events(events)
                merged = save_settings(data)
            except (ValueError, TypeError, OSError) as e:
                # Validation messages are intentionally actionable (notably the
                # Mac project root); no secret value is ever included in them.
                self._send(400, json.dumps({"ok": False, "output": str(e) or "bad settings payload"}))
                return
            self._send(200, json.dumps(settings_response(merged)))
        elif p == "/api/linear-issue":
            title = str(data.get("title", "")).strip()
            if not title:
                self._send(400, json.dumps({"ok": False, "output": "title required"}))
                return
            self._send(200, json.dumps(linear_create(title)))
        elif p == "/api/push-subscribe":
            self._send(200, json.dumps(push_subscribe(data)))
        elif p == "/api/push-unsubscribe":
            self._send(200, json.dumps(push_unsubscribe(data)))
        elif p == "/api/push-send":
            # grave notify's push leg (loopback) and the ⚙️ test path. 502 on
            # zero deliveries so grave's `curl -f` sees the truth.
            result = push_send(data)
            self._send(200 if result["ok"] else 502, json.dumps(result))
        elif p == "/api/notify-test":
            if PORTABLE:
                self._send(404, '{"error":"unavailable in portable workspace"}')
                return
            # Exercise THE production path end-to-end: grave notify fans out to
            # ntfy and (via loopback push-send) back through this dashboard.
            rc, out, err = sh([GRAVE, "notify", "--priority", "default",
                               "📣 test: the box can reach you",
                               f"sent from ⚙️ settings on {HOST}"], timeout=30)
            self._send(200, json.dumps({"ok": rc == 0,
                                        "output": ANSI.sub("", (out + err)).strip()}))
        elif p == "/api/session-kill":
            name = str(data.get("name", ""))
            if not re.fullmatch(r"[A-Za-z0-9_-]{1,50}", name):
                self._send(400, json.dumps({"ok": False, "output": "bad session name"}))
                return
            rc, out, err = sh(["tmux", "-L", TMUX_SOCKET, "kill-session", "-t", name])
            self._send(200, json.dumps({"ok": rc == 0, "output": out + err}))
        elif p == "/api/session-resume":
            if PORTABLE:
                self._send(404, '{"error":"unavailable in portable workspace"}')
                return
            # ▶ on a dead session (#110): grave recreates it in its recorded
            # dir (meta.json) and re-attaches the pipe-pane log.
            name = str(data.get("name", ""))
            if not re.fullmatch(r"[A-Za-z0-9_-]{1,50}", name):
                self._send(400, json.dumps({"ok": False, "output": "bad session name"}))
                return
            rc, out, err = sh([GRAVE, "agents", "resume", name], timeout=30)
            self._send(200, json.dumps({"ok": rc == 0,
                                        "output": ANSI.sub("", out + err).strip()}))
        elif p == "/api/session-capture":
            # Session scrollback into a textarea — the copy path that works
            # even where the in-terminal one can't (no touch selection in
            # xterm.js, WebKit clipboard gesture rules). See docs/TERMINAL.md.
            name = str(data.get("name", ""))
            if not re.fullmatch(r"[A-Za-z0-9_-]{1,50}", name):
                self._send(400, json.dumps({"ok": False, "output": "bad session name"}))
                return
            # =name: exact-match target (bare names prefix-match). -J joins
            # wrapped lines; -S -2000 reaches back into scrollback.
            rc, out, err = sh(["tmux", "-L", TMUX_SOCKET, "capture-pane", "-p", "-J",
                               "-t", "=" + name, "-S", "-2000"])
            self._send(200, json.dumps(
                {"ok": rc == 0, "output": out if rc == 0 else out + err}))
        elif p == "/api/admin/upgrade":
            if PORTABLE:
                self._send(404, '{"error":"unavailable in portable workspace"}')
                return
            if MACOS:
                if set(data) == {"tag"} and isinstance(data["tag"], str) and re.fullmatch(r"v[0-9]+\.[0-9]+\.[0-9]+", data["tag"]): cmd = [MACOS_GRAVE, "upgrade", "--tag", data["tag"]]
                elif set(data) == {"channel"} and data["channel"] in ("release", "edge"): cmd = [MACOS_GRAVE, "upgrade", "--" + data["channel"]]
                else: self._send(400, json.dumps({"ok":False,"output":"invalid release request"})); return
                rc, out, err = sh(cmd, timeout=15)
            else:
                tag = str(data.get("tag", ""))
                if not re.fullmatch(r"v[0-9]+\.[0-9]+\.[0-9]+", tag): self._send(400, json.dumps({"ok":False,"output":"invalid release tag"})); return
                unit = f"gravedecay-upgrade@{tag}.service"; rc, out, err = sh(["sudo", "-n", "systemctl", "--no-block", "start", unit])
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



if __name__ == "__main__":
    ThreadingHTTPServer((BIND_HOST, PORT), Handler).serve_forever()
