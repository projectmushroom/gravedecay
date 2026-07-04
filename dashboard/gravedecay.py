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
import io
import json
import os
import re
import shutil
import socket
import subprocess
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(os.environ.get("GRAVEDECAY_PORT", "4712"))
GRAVE_ROOT = os.environ.get("GRAVE_ROOT", "/srv/dev")
# Mount prefix when path-routed behind `tailscale serve --set-path` on the same
# origin as T3 (single entry point). Bare paths keep working for localhost.
BASE = os.environ.get("GRAVEDECAY_BASE", "/dash").rstrip("/")
ICON_PATH = os.environ.get("GRAVEDECAY_ICON", os.path.join(GRAVE_ROOT, "config", "gravedecay.png"))
HOST = socket.gethostname()
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
    "panel_order": ["prs", "reviews", "ci", "linear", "usage", "tmux", "repos",
                    "stats", "actions", "services", "docker", "journal"],
    "hidden_panels": [],   # panel ids to hide
    "hidden_apps": [],     # launcher tile names to hide
    "newtab_apps": [],     # tile names that open in a new tab instead of in-PWA
    "custom_apps": [],     # extra tiles: [{"name": ..., "url": ...}]
    "poll_ms": 5000,       # dashboard refresh interval
}


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
    return s


def save_settings(data):
    merged = load_settings()
    for k, default in DEFAULT_SETTINGS.items():
        if k in data and isinstance(data[k], type(default)):
            merged[k] = data[k]
    merged["poll_ms"] = max(2000, min(60000, int(merged["poll_ms"])))
    merged["custom_apps"] = [
        {"name": str(a.get("name", "app"))[:40], "url": str(a.get("url", ""))[:200]}
        for a in merged["custom_apps"] if isinstance(a, dict) and a.get("url")][:12]
    tmp = SETTINGS_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(merged, f, indent=2)
    os.replace(tmp, SETTINGS_PATH)
    return merged


GRAVE = "/usr/local/bin/grave"
# grave runs AS THE SERVICE USER (it sudo -n's internally where needed):
# under sudo it would be root, whose tmux lives in /tmp/tmux-0 — freeze/kill
# of agent sessions would silently no-op.
ACTIONS = {
    "gaming": [GRAVE, "gaming"],                 # 🧊 freeze sessions
    "gaming-kill": [GRAVE, "gaming", "--kill"],  # ☠️ destroy them
    "developer": [GRAVE, "developer"],
    "restart-t3": ["sudo", "-n", "systemctl", "restart", "t3code"],
    "doctor": [GRAVE, "doctor"],
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
# mounted (https://box/dash/) without caring which.
MANIFEST = json.dumps({
    "name": "gravedecay", "short_name": "gravedecay", "start_url": "./", "scope": "./",
    "display": "standalone", "background_color": "#070907", "theme_color": "#070907",
    "icons": [{"src": "icon-192.png", "sizes": "192x192", "type": "image/png"},
              {"src": "icon-512.png", "sizes": "512x512", "type": "image/png"}],
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
        return {"login": login, "error": None,
                "prs": search("--owner", login), "more_url": more,
                "reviews": search(f"--review-requested={login}"),
                "reviews_url": "https://github.com/pulls/review-requested"}
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
            "tmux": tmux, "torpor": len(tmux) if frozen else 0,
            "system": collect_system(),
            "github": {"login": None, "prs": [], "reviews": [], "error": "paused in game mode"},
            "linear": {"configured": False, "issues": [], "error": None},
            "ci": {"rows": []}, "usage": None, "services": [], "repos": [],
            "docker": {"error": "docker stopped (gaming)", "containers": []},
            "journal": [], "backups": {"count": 0, "latest": None},
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


class Handler(BaseHTTPRequestHandler):
    server_version = "gravedecay/1"
    # HTTP/1.1: _send always sets Content-Length (keep-alive works), and the
    # SSE stream omits it so the handler auto-closes the connection at end.
    protocol_version = "HTTP/1.1"

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
        qs = self.path.split("?", 1)[1] if "?" in self.path else ""
        action = dict(kv.split("=", 1) for kv in qs.split("&") if "=" in kv).get("action", "")
        cmd = ACTIONS.get(action)
        if not cmd:
            self._send(400, json.dumps({"ok": False, "output": "unknown action"}))
            return
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
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, bufsize=1)
        sent = 0
        try:
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
        finally:
            ACTION_LOCK.release()

    def do_GET(self):
        p = self._route()
        if p is None:
            return
        if p == "/api/action-stream":
            self._stream_action()
            return
        if p == "/healthz":
            self._send(200, '{"ok":true}')
        elif p == "/api/state":
            self._send(200, json.dumps(state(self.headers)))
        elif p == "/":
            boot = json.dumps(state(self.headers)).replace("</", "<\\/")
            self._send(200, PAGE.replace("/*BOOT*/null", boot), "text/html; charset=utf-8")
        elif p == "/manifest.webmanifest":
            self._send(200, MANIFEST, "application/manifest+json")
        elif p in ("/apple-touch-icon.png", "/icon-180.png"):
            self._send(200, icon_png(180), "image/png")
        elif p == "/icon-192.png":
            self._send(200, icon_png(192), "image/png")
        elif p == "/icon-512.png":
            self._send(200, icon_png(512), "image/png")
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
        try:
            length = int(self.headers.get("Content-Length", 0))
            data = json.loads(self.rfile.read(length)) if length else {}
        except ValueError:
            self._send(400, json.dumps({"ok": False, "output": "bad payload"}))
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
            rc, out, err = sh(["tmux", "-L", "agents", "kill-session", "-t", name])
            self._send(200, json.dumps({"ok": rc == 0, "output": out + err}))
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
// (https://box/dash), every relative URL on this page — manifest, icons,
// api/state, api/action-stream — resolves against the ORIGIN ROOT and lands
// on T3 instead of this dashboard. The backend cannot 301 it because
// tailscale serve strips the mount prefix before proxying. Fix the base
// before the parser touches any href/src below.
if(location.pathname==='@BASE@')history.replaceState(null,'','@BASE@/');
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
  padding:0 14px calc(24px + env(safe-area-inset-bottom));max-width:1120px;margin:0 auto}
/* CRT: fixed scanlines + vignette, zero layout cost */
body::before{content:'';position:fixed;inset:0;pointer-events:none;z-index:99;
  background:repeating-linear-gradient(0deg,transparent 0 2px,rgba(0,0,0,.13) 2px 3px)}
body::after{content:'';position:fixed;inset:0;pointer-events:none;z-index:98;
  background:radial-gradient(ellipse at 50% 40%,transparent 60%,rgba(0,0,0,.32))}
::selection{background:var(--accent);color:#000}
a{color:var(--accent);text-decoration:none}
a:hover{background:var(--accent);color:#000}
h1{font-size:15px;font-weight:700;color:var(--ink);text-shadow:var(--glow)}
h1::before{content:'> ';color:var(--accent)}
.topbar{position:sticky;top:0;z-index:10;display:flex;flex-wrap:wrap;gap:10px;align-items:center;
  background:var(--page);margin:0 -14px 16px;
  padding:calc(10px + env(safe-area-inset-top)) 14px 8px;
  border-bottom:1px solid var(--ring)}
.topbar .meta{color:var(--muted);font-size:12px;margin-left:auto}
.badge{display:inline-flex;align-items:center;gap:6px;padding:2px 10px;
  border:1px solid var(--ring);font-size:12px;font-weight:700;color:var(--ink)}
#mode:hover{border-color:var(--accent);color:var(--accent)}
/* launcher tiles */
.apps{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:8px;margin-bottom:14px}
.app{display:flex;align-items:center;justify-content:center;gap:8px;min-height:52px;
  background:var(--surface);border:1px solid var(--ring);
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
@media(max-width:760px){#panels{grid-template-columns:1fr}}
.w-full{grid-column:1/-1}
.panel{position:relative;background:var(--surface);border:1px solid var(--ring);
  border-radius:0;padding:16px 12px 10px}
.panel h2{position:absolute;top:-8px;left:10px;background:var(--page);padding:0 7px;
  font-size:11px;font-weight:700;color:var(--title);letter-spacing:.08em;
  text-transform:uppercase}
/* stat tiles */
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(148px,1fr));gap:10px;
  background:transparent;border:none;padding:0}
.tile{background:var(--surface);border:1px solid var(--ring);padding:10px 12px}
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
table{width:100%;border-collapse:collapse;font-size:13px}
td{padding:4px 8px 4px 0;border-top:1px dashed var(--hairline);vertical-align:top}
tr:first-child td{border-top:none}
td.num{text-align:right;font-variant-numeric:tabular-nums;color:var(--ink-2)}
td.dim{color:var(--muted)}
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
  backdrop-filter:blur(3px);-webkit-backdrop-filter:blur(3px);padding:14px;overflow-y:auto}
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
#settings-panel{display:none;margin-bottom:16px}
.setrow{display:flex;gap:8px;align-items:center;margin:7px 0;flex-wrap:wrap;font-size:13px}
.setrow input,.setrow select{background:var(--inset);border:1px solid var(--hairline);
  color:var(--ink);border-radius:0;padding:7px 9px;
  font:13px ui-monospace,Menlo,monospace}
.setrow input:focus,.setrow select:focus{outline:1px solid var(--accent)}
.mini{min-height:28px;padding:2px 9px;font-size:12px}
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
</style></head><body>
<div class="topbar">
  <h1>gravedecay</h1>
  <span class="badge" id="mode" role="button" title="Tap to switch mode" style="cursor:pointer">…</span>
  <button class="gear" id="gear" title="Settings" aria-label="Settings">⚙️</button>
  <span class="meta" id="meta">connecting…</span>
</div>
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
<div class="panel" id="settings-panel">
  <h2>⚙️ Settings</h2>
  <div class="sethead">Widgets — show &amp; order</div>
  <div id="set-widgets"></div>
  <div class="sethead">Launcher tiles</div>
  <div id="set-apps"></div>
  <div class="setrow">
    <input id="new-app-name" placeholder="label (e.g. 🎬 Jellyfin)" size="18">
    <input id="new-app-url" placeholder="/path or https://…" size="22">
    <button class="mini" id="add-app">＋ add tile</button>
  </div>
  <div class="sethead">Auth — each opens a terminal running the login flow</div>
  <div class="setrow">
    <a class="mini abtn" data-auth="auth-claude">🤖 Re-auth Claude</a>
    <a class="mini abtn" data-auth="auth-codex">🧠 Re-auth Codex</a>
    <a class="mini abtn" data-auth="auth-github">🐙 Re-auth GitHub</a>
  </div>
  <div class="sethead">Integrations</div>
  <div class="setrow"><span class="setlabel">Linear API key <span id="linear-state"></span></span>
    <input type="password" id="set-linear" placeholder="lin_api_… (leave empty to keep)" size="26">
  </div>
  <div class="sethead">Refresh</div>
  <div class="setrow"><span class="setlabel">poll interval</span>
    <select id="set-poll">
      <option value="2000">2 s</option><option value="5000">5 s</option>
      <option value="10000">10 s</option><option value="30000">30 s</option>
    </select>
  </div>
  <div class="setrow"><button id="save-set">💾 Save</button>
    <button class="mini" id="close-set">Close</button><span id="set-msg" class="setlabel"></span></div>
</div>
<div class="overlay" id="console" style="display:none">
  <div class="dlg">
    <button class="mini" id="console-x" title="close (Esc)" aria-label="close"
      style="position:absolute;top:10px;right:10px;z-index:2">✕</button>
    <div id="console-title">▚ grave</div>
    <pre id="console-out"></pre><span id="ccur">▮</span>
    <div class="setrow"><button class="mini" id="console-close" style="display:none">close</button></div>
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
      <button class="mini" id="gc-cancel">Cancel</button>
    </div>
  </div>
</div>
<div id="panels">
  <div class="panel" data-panel="prs"><h2>🔀 Open pull requests</h2><table id="prs"></table></div>
  <div class="panel" data-panel="reviews"><h2>👀 Review requests</h2><table id="reviews"></table></div>
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
      <button data-act="doctor">🩺 Run doctor</button>
    </div>
  </div>
  <div class="panel" data-panel="services"><h2>⚙️ Services</h2><table id="services"></table></div>
  <div class="panel" data-panel="docker"><h2>🐳 Docker</h2><table id="docker"></table></div>
  <div class="panel w-full" data-panel="journal"><h2>📋 Journal errors (24 h)</h2><pre id="journal"></pre></div>
</div>
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
// same-origin app paths need the https origin spelled out when gravedecay is
// viewed on a bare port (localhost:4712) rather than mounted at /dash/
const appUrl=u=>(location.port&&location.port!=='443'&&u.startsWith('/'))
  ?`https://${location.hostname}${u}`:u;
const PANEL_NAMES={prs:'Open pull requests',reviews:'Review requests',ci:'CI status',
  linear:'Linear issues',usage:'Agent usage',tmux:'Agent sessions',repos:'Repos',
  stats:'Stats tiles',actions:'Actions',services:'Services',docker:'Docker',
  journal:'Journal errors'};
const PANEL_TABS={prs:'work',reviews:'work',ci:'work',linear:'work',usage:'work',
  tmux:'work',repos:'work',
  stats:'system',actions:'system',services:'system',docker:'system',journal:'system'};
let linearConfigured=false,lastMode=null,lastTmux=[];
let cfg=null,envApps=[],layoutKey='';
let activeTab=localStorage.getItem('grave-tab')||'work';
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
});
function render(s){
  envApps=s.apps||[];
  lastTmux=s.tmux||[];
  const modeChanged=lastMode!==null&&lastMode!==s.mode;
  lastMode=s.mode;
  document.body.classList.toggle('gaming',s.mode==='gaming');
  $('game-banner').style.display=s.mode==='gaming'?'block':'none';
  if(s.mode==='gaming')
    $('torpor-line').textContent=s.torpor
      ?`dev stack buried · 🧊 ${s.torpor} agent session${s.torpor>1?'s':''} in torpor (RAM kept, zero CPU)`
      :'dev stack buried · no agent sessions held';
  if(!cfg){cfg=s.settings;schedule();}
  if(modeChanged)schedule();
  const k=JSON.stringify([cfg.panel_order,cfg.hidden_panels,activeTab]);
  if(k!==layoutKey){layoutKey=k;applyLayout();}
  $('apps').innerHTML=allApps().filter(a=>!cfg.hidden_apps.includes(a.name)).map(a=>
    `<a class="app" href="${esc(appUrl(a.url))}"${
      (cfg.newtab_apps||[]).includes(a.name)?' target="_blank" rel="noopener"':''
    }>${esc(a.name)}</a>`).join('');
  $('mode').textContent=(s.mode==='developer'?'💻 developer':'🎮 gaming');
  $('meta').textContent=`up ${fmtUp(s.system.uptime_s)} · ${s.now}`;
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
  $('prs').innerHTML=gh.error
    ? `<tr><td class="dim">${esc(gh.error)}</td></tr>`
    : ((gh.prs||[]).slice(0,5).map(p=>`<tr>
        <td><a href="${esc(p.url)}" target="_blank" rel="noopener">${esc(p.repo)} #${p.number}</a></td>
        <td class="dim">${esc(p.title)}</td></tr>`).join('')
       +moreRow(5,(gh.prs||[]).length,gh.more_url)
       ||'<tr><td class="dim">no open PRs 🎉</td></tr>');
  $('reviews').innerHTML=gh.error
    ? `<tr><td class="dim">${esc(gh.error)}</td></tr>`
    : ((gh.reviews||[]).slice(0,5).map(p=>`<tr>
        <td><a href="${esc(p.url)}" target="_blank" rel="noopener">${esc(p.repo)} #${p.number}</a></td>
        <td class="dim">${esc(p.title)}</td></tr>`).join('')
       +moreRow(5,(gh.reviews||[]).length,gh.reviews_url)
       ||'<tr><td class="dim">nobody is waiting on you 🎉</td></tr>');
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
}
async function poll(){
  if(document.hidden)return;
  try{
    const r=await fetch('api/state');
    render(await r.json());
  }catch(e){ $('meta').textContent='unreachable — retrying'; }
}
// ---------- boot console (fetch-streamed SSE) ----------
// fetch instead of EventSource: real HTTP errors become readable (through
// EventSource a 403 and a dead box look identical) and there is no browser
// auto-retry that could silently re-trigger a mode switch.
let streaming=false,aborter=null,runId=0;
function closeConsole(){
  // closing only detaches the console — the action always finishes on the box
  if(aborter){aborter.abort();aborter=null;}
  $('console').style.display='none';
}
async function runStream(act,title){
  if(streaming)return;
  streaming=true;
  const myRun=++runId;
  aborter=new AbortController();
  $('game-confirm').style.display='none';
  $('console').style.display='block';
  $('console-title').textContent='▚ '+(title||('grave '+act));
  const out=$('console-out');out.innerHTML='';
  $('console-close').style.display='none';
  const add=(t,cls)=>{const d=document.createElement('div');
    d.className=cls||(t.includes('✗')?'err':(/🎮|💻|🪦|Gaming|Developer/.test(t)?'hl':''));
    d.textContent=t||' ';out.appendChild(d);out.scrollTop=out.scrollHeight;};
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
      let buf='';
      for(;;){
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
          }else if(da){gotData=true;add(JSON.parse(da[1]));}
        }
      }
      if(!gotData)add('— connection closed before any output arrived','err');
    }
  }catch(e){
    if(e.name!=='AbortError')
      add(gotData?'— stream lost: '+e+' (the action keeps running on the box)'
                 :'— request failed: '+e,'err');
  }
  streaming=false;aborter=null;
  $('console-close').style.display='';
  poll();
  // success: linger briefly so the ✓ registers, then get out of the way
  if(okDone)setTimeout(()=>{if(runId===myRun)closeConsole();},3500);
}
$('console-x').onclick=closeConsole;
$('console').addEventListener('click',e=>{if(e.target.id==='console')closeConsole();});
$('game-confirm').addEventListener('click',e=>{
  if(e.target.id==='game-confirm')e.currentTarget.style.display='none';});
document.addEventListener('keydown',e=>{if(e.key==='Escape'){
  closeConsole();$('game-confirm').style.display='none';}});
$('console-close').onclick=()=>{$('console').style.display='none'};
// ---------- gaming confirm dialog ----------
function openGameConfirm(){
  $('gc-sessions').innerHTML=lastTmux.length
    ?`Agent sessions: <b>${lastTmux.map(x=>esc(x.name)).join(', ')}</b><br>
      <span class="dim2">🧊 keeps them frozen in RAM (zero CPU, thawed on wake) · ☠️ destroys them for maximum free RAM</span>`
    :'<span class="dim2">No agent sessions running — both options behave the same.</span>';
  $('game-confirm').style.display='block';
}
$('gc-freeze').onclick=()=>runStream('gaming','burial sequence — freeze');
$('gc-kill').onclick=()=>runStream('gaming-kill','burial sequence — full kill');
$('gc-cancel').onclick=()=>{$('game-confirm').style.display='none'};
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
      <span title="open in a new tab instead of inside the PWA">↗ new tab</span></div>`
    +draftApps.map((a,i)=>`<div class="setrow">
    <input type="checkbox" data-app-vis="${esc(a.name)}" ${draft.hidden_apps.includes(a.name)?'':'checked'}>
    <span class="setlabel">${esc(a.name)} <span style="color:var(--muted)">${esc(a.url)}</span></span>
    <label title="open in a new tab"><input type="checkbox" data-app-newtab="${esc(a.name)}" ${(draft.newtab_apps||[]).includes(a.name)?'checked':''}> ↗</label>
    ${i>=envApps.length?`<button class="mini" data-del-app="${i-envApps.length}">✕</button>`:''}
  </div>`).join('');
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
  draft.newtab_apps=[...document.querySelectorAll('[data-app-newtab]')]
    .filter(c=>c.checked).map(c=>c.dataset.appNewtab);
  draft.poll_ms=+$('set-poll').value;
}
$('gear').onclick=()=>{
  const p=$('settings-panel');
  if(p.style.display==='block'){p.style.display='none';return;}
  if(!cfg)return;
  buildSettings();p.style.display='block';
};
$('close-set').onclick=()=>{$('settings-panel').style.display='none'};
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
    if(j.ok){cfg=j.settings;layoutKey='';schedule();
      linearConfigured=!!j.linear_configured;
      $('set-linear').value='';
      $('linear-state').textContent=linearConfigured?'✓ configured':'(not set)';
      $('set-msg').textContent='saved ✓';poll();}
    else $('set-msg').textContent=j.output||'save failed';
  }catch(e){$('set-msg').textContent='save failed: '+e;}
};
// ---------- boot ----------
let timer=null;
function schedule(){clearInterval(timer);
  // game mode: back off to 30s — the dashboard must not cost resources
  timer=setInterval(poll,lastMode==='gaming'?30000:((cfg&&cfg.poll_ms)||5000));}
const BOOT=/*BOOT*/null;   // server-rendered initial state: instant first paint
if(BOOT)render(BOOT);else poll();
schedule();
document.addEventListener('visibilitychange',()=>{if(!document.hidden)poll()});
</script></body></html>
""".replace("@HOST@", HOST).replace("@BASE@", BASE or "/dash")

if __name__ == "__main__":
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
