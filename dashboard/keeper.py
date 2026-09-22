"""The Gravekeeper runner: the installed provider CLI per turn, normalised events, owner-private index.

Also the Keeper MCP server (``keeper.py mcp``), the doctor probe (``keeper.py check``)
and the shell client behind ``grave keeper ask|resume``. Stdlib only.
"""
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import uuid
from collections import deque
from pathlib import Path

PROVIDERS = ("claude", "codex")
RETENTION = 30 * 86400
KEY_AGE = 86400
MAX_CONVERSATIONS = 64
MAX_EVENTS = 400
MAX_TEXT = 16 * 1024
MAX_OUTPUT = 256 * 1024
MAX_MESSAGE = 8000
TURN_TIMEOUT = 600
FINAL = ("succeeded", "failed", "timed_out", "cancelled", "interrupted")
STATES = ("idle", "running", *FINAL)
EVENT_TYPES = ("text", "tool_call", "tool_result", "done", "error")
RESOURCES = ("system", "services", "containers", "sessions", "repositories", "preferences")
LOG_TARGETS = ("t3", "dash", "term", "ssh", "tailscale", "system", "grave")  # grave logs targets without a name argument
SECRET_ENV = re.compile(r"TOKEN|SECRET|PASSWORD|CREDENTIAL|_KEY$|^KEY_|APIKEY", re.I)
ID_PATTERN = re.compile(r"[0-9]{10}-[0-9a-f]{32}")
BUILD_ID = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
GRAVE_ROOT = os.environ.get("GRAVE_ROOT", "/srv/dev")
GRAVE_CONF = os.environ.get("GRAVE_CONF", "/etc/gravedecay/grave.conf")


class KeeperError(Exception):
    def __init__(self, status, code, message):
        super().__init__(message)
        self.status, self.code = status, code


def new_id():
    return str(int(time.time())) + "-" + uuid.uuid4().hex


def conf():
    """KEEPER_PROVIDER / KEEPER_MODEL from grave.conf; empty model means the CLI default."""
    values = {"provider": "claude", "model": ""}
    try:
        with open(GRAVE_CONF) as stream:
            for line in stream:
                match = re.match(r'KEEPER_(PROVIDER|MODEL)="?([A-Za-z0-9._:\[\]-]*)"?\s*$', line.strip())
                if match:
                    values[match.group(1).lower()] = match.group(2)
    except OSError:
        pass
    if values["provider"] not in PROVIDERS:
        values["provider"] = "claude"
    return values


def scrub_env(env):
    return {k: v for k, v in env.items() if not SECRET_ENV.search(k)}


def clip(value):
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    return text.encode("utf-8")[:MAX_TEXT].decode("utf-8", "ignore")


def blocks_text(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")
    return ""


def claude_events(obj):
    """One stream-json line -> (normalised events, session_id or None)."""
    events, session = [], None
    kind = obj.get("type")
    if kind == "system" and obj.get("subtype") == "init":
        session = obj.get("session_id")
    elif kind in ("assistant", "user"):
        for block in (obj.get("message") or {}).get("content") or []:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text" and block.get("text"):
                events.append({"type": "text", "text": block["text"]})
            elif block.get("type") == "tool_use":
                events.append({"type": "tool_call", "name": str(block.get("name", "")), "id": str(block.get("id", "")),
                               "text": clip(block.get("input", {}))})
            elif block.get("type") == "tool_result":
                # The runner replaces the tool_use_id with the tool's name.
                events.append({"type": "tool_result", "name": str(block.get("tool_use_id", "")), "text": blocks_text(block.get("content"))})
    elif kind == "result":
        session = obj.get("session_id")
        if obj.get("is_error") or obj.get("subtype") not in (None, "success"):
            events.append({"type": "error", "text": blocks_text(obj.get("result")) or str(obj.get("subtype", "provider error"))})
        else:
            events.append({"type": "done", "text": ""})
    return events, session


def codex_events(obj):
    """One ``codex exec --json`` line -> (normalised events, thread_id or None)."""
    events, session = [], None
    kind = obj.get("type")
    item = obj.get("item") if isinstance(obj.get("item"), dict) else {}
    if kind == "thread.started":
        session = obj.get("thread_id")
    elif kind == "item.completed" and item.get("type") == "agent_message":
        if item.get("text"):
            events.append({"type": "text", "text": item["text"]})
    elif kind == "item.completed" and item.get("type") == "error":
        events.append({"type": "error", "text": str(item.get("message", "provider error"))})
    elif kind in ("item.started", "item.completed") and item.get("type") not in ("agent_message", "reasoning", "error", None):
        if item.get("type") == "mcp_tool_call":
            name = f"{item.get('server', '')}__{item.get('tool', '')}"
        else:
            name = str(item.get("type"))
        if kind == "item.started":
            events.append({"type": "tool_call", "name": name, "text": clip(item.get("arguments", item.get("command", "")))})
        else:
            result = item.get("error") or item.get("result") or item.get("aggregated_output") or ""
            if isinstance(result, dict):
                result = blocks_text(result.get("content")) or result.get("message") or clip(result)
            events.append({"type": "tool_result", "name": name, "text": clip(result)})
    elif kind == "turn.completed":
        events.append({"type": "done", "text": ""})
    elif kind == "turn.failed":
        events.append({"type": "error", "text": str((obj.get("error") or {}).get("message", "turn failed"))})
    elif kind == "error":
        events.append({"type": "error", "text": str(obj.get("message", "provider error"))})
    return events, session


MAPPERS = {"claude": claude_events, "codex": codex_events}


class Runner:
    """One conversation index file per conversation; the provider owns the transcript."""

    def __init__(self, path, host, script=__file__, grave="/usr/local/bin/grave",
                 api="http://127.0.0.1:4712/grave", work_dir=None, timeout=TURN_TIMEOUT):
        self.path = Path(path)
        self.host = host
        self.script = str(script)
        self.grave = grave
        self.api = api
        self.work_dir = work_dir or os.path.join(GRAVE_ROOT, "config", "keeper")
        self.timeout = timeout
        self.instance = uuid.uuid4().hex
        self.mutex = threading.Lock()
        self.locks = {}
        self.processes = {}
        self.cancelled = set()

    # ---- storage ----
    def _path(self, value):
        if not isinstance(value, str) or not ID_PATTERN.fullmatch(value):
            raise KeeperError(400, "invalid_id", "Use <Unix seconds>-<32 lowercase UUID hex digits>")
        return self.path / (value + ".json")

    def _read(self, path):
        try:
            with path.open() as stream:
                record = json.loads(stream.read(2 * 1024 * 1024))
            if (not isinstance(record, dict) or self._path(record["id"]) != path
                    or record["provider"] not in PROVIDERS or record["state"] not in STATES
                    or not isinstance(record["events"], list) or len(record["events"]) > MAX_EVENTS
                    or not {"session_id", "host", "owner", "created_at", "updated_at", "summary", "turn",
                            "cursor", "truncated", "message", "instance"} <= record.keys()
                    or any(e["type"] not in EVENT_TYPES or not isinstance(e["seq"], int) or not isinstance(e["text"], str)
                           for e in record["events"])):
                raise ValueError("Invalid conversation record")
            return record
        except FileNotFoundError:
            return None
        except (ValueError, KeyError, TypeError, KeeperError) as error:
            raise OSError("Unreadable conversation record") from error

    def _save(self, record):
        self.path.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd, name = tempfile.mkstemp(dir=self.path, prefix=".conversation-")
        try:
            with os.fdopen(fd, "w") as stream:
                json.dump(record, stream)
            os.replace(name, self._path(record["id"]))
        finally:
            if os.path.exists(name):
                os.unlink(name)

    def _recover(self, record):
        if record["state"] == "running" and (record["instance"] != self.instance or record["id"] not in self.processes):
            record.update(state="interrupted", updated_at=time.time(),
                          message="Dashboard restarted during this turn; outcome unknown. Resume to ask again.")
            if record["turn"]:
                record["turn"]["finished_at"] = record["updated_at"]
            self._save(record)
        return record

    def _load(self, conversation_id, owner):
        record = self._read(self._path(conversation_id))
        if record is None or record["owner"] != owner:
            raise KeeperError(404, "not_found", "Conversation not found for this owner, or expired")
        return self._recover(record)

    @staticmethod
    def _public(record, after=0):
        return {k: v for k, v in record.items() if k not in ("instance", "events", "bytes")} | {
            "events": [event for event in record["events"] if event["seq"] > after]}

    # ---- API ----
    def list(self, owner):
        rows = []
        with self.mutex:
            for path in sorted(self.path.glob("*.json"), reverse=True):
                record = self._read(path)
                if record and record["owner"] == owner:
                    record = self._recover(record)
                    rows.append({k: record[k] for k in ("id", "provider", "session_id", "host", "owner", "created_at",
                                                        "updated_at", "summary", "state")})
        return rows

    def get(self, conversation_id, owner, after=0):
        if not isinstance(after, int) or after < 0:
            raise KeeperError(400, "invalid_cursor", "Cursor must be a nonnegative integer")
        with self.mutex:
            return self._public(self._load(conversation_id, owner), after)

    def create(self, conversation_id, owner, provider=None):
        path = self._path(conversation_id)
        settings = conf()
        provider = provider or settings["provider"]
        if provider not in PROVIDERS:
            raise KeeperError(400, "invalid_provider", "Provider must be one of " + ", ".join(PROVIDERS))
        with self.mutex:
            record = self._read(path)
            if record:
                if record["owner"] != owner:
                    raise KeeperError(404, "not_found", "Conversation not found for this owner, or expired")
                return self._public(self._recover(record)), False
            now = time.time()
            if not now - KEY_AGE <= int(conversation_id.split("-")[0]) <= now + 300:
                raise KeeperError(409, "expired_id", "Unknown conversation ID is too old or in the future")
            for old in list(self.path.glob("*.json")):
                if int(old.stem.split("-")[0]) < now - RETENTION:
                    old.unlink()
            if len(list(self.path.glob("*.json"))) >= MAX_CONVERSATIONS:
                raise KeeperError(503, "history_full", "Conversation history is full; wait for old conversations to expire")
            record = {"id": conversation_id, "provider": provider, "model": settings["model"], "session_id": None,
                      "host": self.host, "owner": owner, "created_at": now, "updated_at": now, "summary": "",
                      "state": "idle", "turn": None, "cursor": 0, "events": [], "bytes": 0, "truncated": False,
                      "message": "", "instance": self.instance}
            self._save(record)
            return self._public(record), True

    def send(self, conversation_id, turn_id, message, owner):
        self._path(turn_id)
        if not isinstance(message, str) or not message.strip() or len(message) > MAX_MESSAGE:
            raise KeeperError(400, "invalid_message", f"Message must be 1 to {MAX_MESSAGE} characters")
        with self.mutex:
            record = self._load(conversation_id, owner)
            if record["turn"] and record["turn"]["id"] == turn_id:
                return self._public(record), False
            if record["state"] == "running":
                raise KeeperError(409, "busy", "A turn is already running in this conversation")
            now = time.time()
            if not now - KEY_AGE <= int(turn_id.split("-")[0]) <= now + 300:
                raise KeeperError(409, "expired_id", "Unknown turn ID is too old or in the future")
            if len(record["events"]) >= MAX_EVENTS - 2 or record["truncated"]:
                raise KeeperError(409, "conversation_full", "This conversation is full; start a new one")
            lock = self.locks.setdefault(conversation_id, threading.Lock())
            if not lock.acquire(blocking=False):
                raise KeeperError(409, "busy", "A turn is already running in this conversation")
            try:
                record.update(state="running", updated_at=now, message="", instance=self.instance,
                              turn={"id": turn_id, "started_at": now, "finished_at": None, "exit_code": None})
                self._append(record, {"type": "text", "name": "owner", "text": message})
                self._save(record)
                self.processes[conversation_id] = None
                threading.Thread(target=self._run, args=(record, message, lock), daemon=True).start()
            except Exception:
                self.processes.pop(conversation_id, None)
                lock.release()
                raise
            return self._public(record), True

    def cancel(self, conversation_id, owner):
        with self.mutex:
            record = self._load(conversation_id, owner)
            process = self.processes.get(conversation_id)
            if record["state"] == "running" and process:
                self.cancelled.add(conversation_id)
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            return self._public(record)

    def health(self):
        records = list(self.path.glob("*.json"))
        for path in records:
            assert self._read(path) is not None
        return {"ok": True, "protocol": 1, "records": len(records), "build": BUILD_ID, **conf()}

    # ---- provider ----
    def mcp_command(self):
        return [sys.executable, self.script, "mcp", "--root", GRAVE_ROOT, "--api", self.api, "--grave", self.grave]

    def command(self, record):
        """The installed CLI, headless, with the Keeper MCP server added to the owner's own configuration."""
        mcp, provider, model, session = self.mcp_command(), record["provider"], record.get("model") or "", record["session_id"]
        if provider == "claude":
            config = json.dumps({"mcpServers": {"keeper": {"command": mcp[0], "args": mcp[1:]}}})
            argv = ["claude", "-p", "--output-format", "stream-json", "--verbose", "--max-turns", "30",
                    "--mcp-config", config, "--allowedTools", "mcp__keeper"]
            return argv + (["--model", model] if model else []) + (["--resume", session] if session else [])
        argv = ["codex", "exec"] + (["resume", session] if session else ["-C", self.work_dir])
        argv += ["--json", "--skip-git-repo-check", "-c", 'approval_policy="never"', "-c", 'sandbox_mode="read-only"',
                 "-c", f"mcp_servers.keeper.command={json.dumps(mcp[0])}", "-c", f"mcp_servers.keeper.args={json.dumps(mcp[1:])}",
                 "-c", 'mcp_servers.keeper.default_tools_approval_mode="approve"']
        return argv + (["-m", model] if model else []) + ["-"]

    def _append(self, record, event):
        text = event["text"].encode("utf-8")[:MAX_TEXT].decode("utf-8", "ignore")
        if record["bytes"] + len(text.encode("utf-8")) > MAX_OUTPUT or len(record["events"]) >= MAX_EVENTS:
            record["truncated"] = True
            if event["type"] not in ("done", "error"):
                return
            text = ""
        record["bytes"] += len(text.encode("utf-8"))
        record["cursor"] += 1
        record["events"].append({"seq": record["cursor"], "type": event["type"], "name": event.get("name", ""),
                                 "text": text, "at": time.time()})

    def _run(self, record, message, lock):
        # ponytail: one CLI process per turn; a long-lived process per conversation is the upgrade if latency matters.
        process, done, errors, expired, stderr, names = None, False, [], [], deque(maxlen=40), {}

        def kill():
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

        def expire():
            expired.append(True)
            kill()

        try:
            process = subprocess.Popen(self.command(record), cwd=self.work_dir, env=scrub_env(os.environ),
                                       stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                       start_new_session=True)
            with self.mutex:
                self.processes[record["id"]] = process
            threading.Thread(target=lambda: [stderr.append(line.decode("utf-8", "replace")[:500]) for line in process.stderr],
                             daemon=True).start()
            timer = threading.Timer(self.timeout, expire)
            timer.start()
            try:
                try:
                    process.stdin.write(message.encode("utf-8"))
                    process.stdin.close()
                except BrokenPipeError:
                    pass
                for line in process.stdout:
                    try:
                        obj = json.loads(line)
                    except ValueError:
                        continue
                    if not isinstance(obj, dict):
                        continue
                    events, session = MAPPERS[record["provider"]](obj)
                    with self.mutex:
                        if session and not record["session_id"]:
                            record["session_id"] = session
                        for event in events:
                            if event["type"] == "tool_call" and event.get("id"):
                                names[event["id"]] = event["name"]
                            elif event["type"] == "tool_result":
                                event["name"] = names.get(event["name"], event["name"])
                            self._append(record, event)
                            if event["type"] == "text" and event["text"].strip():
                                record["summary"] = event["text"].strip()[:120]
                            done = done or event["type"] == "done"
                            if event["type"] == "error":
                                errors.append(event["text"][:500])
                        record["updated_at"] = time.time()
                        self._save(record)
                rc = process.wait()
            finally:
                timer.cancel()
            timed_out = bool(expired) and not done
            with self.mutex:
                if record["id"] in self.cancelled:
                    record.update(state="cancelled", message="Cancelled by the owner")
                elif timed_out:
                    record.update(state="timed_out", message="Turn exceeded its time limit")
                elif done and rc == 0 and not errors:
                    record["state"] = "succeeded"
                else:
                    record.update(state="failed", message=(errors[-1] if errors else "".join(stderr).strip()[-500:] or f"Provider exited with {rc}"))
                record["turn"]["exit_code"] = rc
        except Exception as error:
            with self.mutex:
                record.update(state="failed", message=f"Could not run the provider: {error}"[:500])
        finally:
            try:
                if process:
                    kill()
                    process.wait()
                    for stream in (process.stdout, process.stderr):
                        if stream:
                            stream.close()
                with self.mutex:
                    record["turn"]["finished_at"] = record["updated_at"] = time.time()
                    self._save(record)
            finally:
                with self.mutex:
                    self.processes.pop(record["id"], None)
                    self.cancelled.discard(record["id"])
                lock.release()


# ---- shell client, MCP server and doctor probe ----
def api_request(api, root, path, data=None):
    with open(os.path.join(root, "config", "secrets", "dashboard-local-token")) as stream:
        token = stream.read(66).strip()
    headers = {"X-Grave-Client": "1", "X-Grave-Local-Token": token, "Content-Type": "application/json"}
    body = json.dumps(data).encode() if data is not None else None
    request = urllib.request.Request(api + "/api/v1/" + path, data=body, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as error:
        try:
            return error.code, json.loads(error.read() or b"{}")
        except ValueError:
            return error.code, {}


def tool_call(name, arguments, root, api, grave):
    if name == "get_resource":
        resource = arguments.get("name")
        if resource not in RESOURCES:
            raise ValueError("Unknown resource; choose one of " + ", ".join(RESOURCES))
        status, result = api_request(api, root, "resources/" + resource)
        if status != 200:
            raise ValueError(result.get("output") or f"Resource read failed with {status}")
        return json.dumps(result, ensure_ascii=False)
    if name == "get_logs":
        target, lines = arguments.get("target"), arguments.get("lines", 100)
        if target not in LOG_TARGETS:
            raise ValueError("Unknown log target; choose one of " + ", ".join(LOG_TARGETS))
        if type(lines) is not int or not 1 <= lines <= 500:
            raise ValueError("lines must be an integer from 1 to 500")
        result = subprocess.run([grave, "logs", "--tail", str(lines), target], capture_output=True, timeout=30)
        return (result.stdout + result.stderr).decode("utf-8", "replace")[-64 * 1024:]
    if name == "run_doctor":
        operation = new_id()
        status, result = api_request(api, root, "operations", {"id": operation, "action": "doctor"})
        if status not in (200, 202):
            raise ValueError(result.get("output") or f"Doctor could not start ({status})")
        deadline, cursor, output = time.monotonic() + 1200, 0, []
        while result["state"] in ("queued", "running") and time.monotonic() < deadline:
            time.sleep(1)
            status, result = api_request(api, root, f"operations?id={operation}&after={cursor}")
            if status != 200:
                raise ValueError(result.get("output") or "Lost track of the doctor operation")
            output += [event["text"] for event in result["events"]]
            cursor = result["cursor"]
        text = re.sub(r"\x1b\[[0-9;]*m", "", "".join(output))
        return f"operation {operation}: {result['state']} (exit {result['exit_code']})\n{text}"[-64 * 1024:]
    raise ValueError("Unknown tool")


TOOLS = [
    {"name": "get_resource", "description": "Read one live management resource of this grave as JSON: " + ", ".join(RESOURCES) + ".",
     "inputSchema": {"type": "object", "properties": {"name": {"type": "string", "enum": list(RESOURCES)}}, "required": ["name"]}},
    {"name": "get_logs", "description": "Recent log lines for one target: " + ", ".join(LOG_TARGETS) + ".",
     "inputSchema": {"type": "object", "properties": {"target": {"type": "string", "enum": list(LOG_TARGETS)},
                                                     "lines": {"type": "integer", "minimum": 1, "maximum": 500}}, "required": ["target"]}},
    {"name": "run_doctor", "description": "Run grave doctor through the durable operation path and return its full report. Takes up to a few minutes.",
     "inputSchema": {"type": "object", "properties": {}}},
]


def mcp_serve(root, api, grave):
    """Newline-delimited JSON-RPC over stdio, as both CLIs speak it."""
    stdin, stdout = sys.stdin.buffer, sys.stdout.buffer

    def reply(request, result=None, error=None):
        if request.get("id") is None:
            return
        body = {"jsonrpc": "2.0", "id": request["id"]}
        body.update({"error": error} if error else {"result": result})
        stdout.write((json.dumps(body, ensure_ascii=False) + "\n").encode())
        stdout.flush()

    for raw in stdin:
        try:
            request = json.loads(raw)
        except ValueError:
            continue
        if not isinstance(request, dict):
            continue
        method = request.get("method")
        if method == "initialize":
            reply(request, {"protocolVersion": request.get("params", {}).get("protocolVersion", "2024-11-05"),
                            "capabilities": {"tools": {}}, "serverInfo": {"name": "keeper", "version": "1"}})
        elif method == "tools/list":
            reply(request, {"tools": TOOLS})
        elif method == "tools/call":
            params = request.get("params") or {}
            try:
                text = tool_call(params.get("name"), params.get("arguments") or {}, root, api, grave)
                reply(request, {"content": [{"type": "text", "text": text}], "isError": False})
            except Exception as error:
                reply(request, {"content": [{"type": "text", "text": str(error)[:2000]}], "isError": True})
        elif method == "ping":
            reply(request, {})
        elif method and not method.startswith("notifications/"):
            reply(request, error={"code": -32601, "message": "Method not found"})


def ask(api, root, prompt, conversation=None):
    """grave keeper ask|resume: drive a turn through the dashboard API and print events as they land."""
    if conversation is None:
        conversation = new_id()
        status, result = api_request(api, root, "keeper", {"id": conversation})
        if status not in (200, 202):
            sys.exit(result.get("output") or f"Could not create conversation ({status})")
    status, result = api_request(api, root, "keeper/turn", {"conversation": conversation, "turn": new_id(), "message": prompt})
    if status not in (200, 202):
        sys.exit(result.get("output") or f"Could not send ({status})")
    print(f"conversation {conversation} ({result['provider']})", file=sys.stderr)
    cursor = 0
    while True:
        status, result = api_request(api, root, f"keeper?id={conversation}&after={cursor}")
        if status != 200:
            sys.exit(result.get("output") or "Lost the conversation")
        for event in result["events"]:
            cursor = event["seq"]
            if event["type"] == "text" and event["name"] != "owner":
                print(event["text"])
            elif event["type"] == "tool_call":
                print(f"→ {event['name']} {event['text'][:200]}", file=sys.stderr)
            elif event["type"] == "tool_result":
                print(f"← {event['text'].splitlines()[0][:200] if event['text'] else ''}", file=sys.stderr)
            elif event["type"] == "error":
                print("error: " + event["text"], file=sys.stderr)
        if result["state"] != "running":
            if result["state"] != "succeeded":
                sys.exit(f"{result['state']}: {result['message']}")
            return
        time.sleep(1)


def check(what, root):
    settings = conf()
    if what == "files":
        assert os.path.isfile(os.path.join(root, "config", "GRAVEKEEPER.md")), "GRAVEKEEPER.md missing; re-run raise.sh"
        for name in ("CLAUDE.md", "AGENTS.md"):
            assert os.path.isfile(os.path.join(root, "config", "keeper", name)), f"config/keeper/{name} missing; re-run raise.sh"
        store = Path(root) / "config" / "secrets" / "keeper"
        if store.exists():
            assert store.stat().st_mode & 0o077 == 0, "keeper storage must be 0700"
            Runner(store, "doctor").health()
    elif what == "provider":
        provider = settings["provider"]
        assert shutil.which(provider), f"{provider} is not installed"
        if provider == "claude":
            result = subprocess.run(["claude", "auth", "status"], capture_output=True, text=True, timeout=30)
            assert result.returncode == 0 and json.loads(result.stdout).get("loggedIn"), "claude is not logged in as the owner"
        else:
            result = subprocess.run(["codex", "login", "status"], capture_output=True, text=True, timeout=30)
            assert result.returncode == 0, "codex is not logged in as the owner"
    else:
        raise SystemExit("usage: keeper.py check files|provider")


def main(argv):
    api = f"http://127.0.0.1:{os.environ.get('DASH_PORT', '4712')}{os.environ.get('GRAVEDECAY_BASE', '/grave').rstrip('/')}"
    if argv[:1] == ["mcp"]:
        options = dict(zip(argv[1::2], argv[2::2]))
        mcp_serve(options.get("--root", GRAVE_ROOT), options.get("--api", api), options.get("--grave", "/usr/local/bin/grave"))
    elif argv[:1] == ["ask"] and len(argv) == 2:
        ask(api, GRAVE_ROOT, argv[1])
    elif argv[:1] == ["resume"] and len(argv) == 3:
        ask(api, GRAVE_ROOT, argv[2], argv[1])
    elif argv[:1] == ["check"] and len(argv) == 2:
        try:
            check(argv[1], GRAVE_ROOT)
        except (AssertionError, OSError, ValueError) as error:
            sys.exit(str(error))
    else:
        sys.exit('usage: grave keeper ask "<prompt>" | resume <conversation> "<prompt>"')


if __name__ == "__main__":
    main(sys.argv[1:])
