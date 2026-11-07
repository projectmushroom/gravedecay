#!/usr/bin/env python3
"""A digger's Slack channel: Socket Mode over one outbound WebSocket. Stdlib only.

``diggers.py serve`` starts this when a definition carries a ``slack`` section and
its tokens are set (docs/DIGGERS.md). Nothing listens: the loop dials Slack, acks
each envelope before any work, and hands allowlisted asks to the runner's queue.
One thread is one conversation. Approvals (Claude) arrive as button clicks on the
same connection; only an allowlisted approver's click answers one.
"""
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import socket
import ssl
import struct
import sys
import threading
import time
import urllib.error
import urllib.request
import urllib.parse
from collections import deque

TEST = os.environ.get("GRAVE_DIGGER_TEST") == "1"
API = (os.environ.get("GRAVE_SLACK_API") if TEST else None) or "https://slack.com"   # the test seam is test-only
TOKEN = {"SLACK_APP_TOKEN": re.compile(r"xapp-[A-Za-z0-9-]{10,250}"), "SLACK_BOT_TOKEN": re.compile(r"xoxb-[A-Za-z0-9-]{10,250}")}
CHANNEL_ID = re.compile(r"[CG][A-Z0-9]{1,20}")
USER_ID = re.compile(r"[UW][A-Z0-9]{1,20}")
KEYS = {"channels", "users", "dm_policy", "require_mention"}
DM_POLICIES = ("deny", "pair", "allow")
GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
FRAME_LIMIT = 1 << 20
IDLE = 30            # seconds of silence before a ping; three in a row reconnect
QUEUE_LIMIT = 5      # Slack asks waiting for the runner
APPROVAL_EXPIRY = 600
PAIR_EXPIRY = 3600
REPLY_LIMIT = 3500
MESSAGE_LIMIT = 8000
GOOD = ("ready-for-review", "no-changes")


class SlackError(Exception):
    pass


def validate(value):
    """Raise ValueError unless ``value`` is a complete binding: deny by default."""
    if not isinstance(value, dict) or not KEYS <= set(value) or set(value) - KEYS - {"approvers"}:
        raise ValueError("slack must hold channels, users, dm_policy, require_mention and optionally approvers")
    for key, pattern in (("channels", CHANNEL_ID), ("users", USER_ID), ("approvers", USER_ID)):
        items = value.get(key, [])
        if not isinstance(items, list) or len(items) > 64 or len(set(items)) != len(items) \
                or not all(isinstance(item, str) and pattern.fullmatch(item) for item in items):
            raise ValueError(f"slack.{key} must list at most 64 distinct Slack IDs ({pattern.pattern})")
    if value["dm_policy"] not in DM_POLICIES:
        raise ValueError("slack.dm_policy must be one of: " + ", ".join(DM_POLICIES))
    if type(value["require_mention"]) is not bool:
        raise ValueError("slack.require_mention must be true or false")
    if not set(approvers(value)) <= set(value["users"]):
        raise ValueError("slack.approvers must be a subset of slack.users")
    return value


def approvers(binding):
    return list(binding.get("approvers", binding["users"]))


def parse_tokens(text):
    """``{"SLACK_APP_TOKEN", "SLACK_BOT_TOKEN"}`` from KEY=value lines or two bare tokens."""
    values = {}
    for line in text.splitlines():
        word = line.strip().partition("=")[2] if "=" in line else line.strip()
        word = word.strip().strip("'\"")
        for key, pattern in TOKEN.items():
            if pattern.fullmatch(word):
                values[key] = word
    if set(values) != set(TOKEN):
        raise SlackError("need one xapp- app token (connections:write) and one xoxb- bot token")
    return values


def render_tokens(values):
    return "".join(f"{key}={values[key]}\n" for key in sorted(TOKEN))


def thread_id(channel, ts, kind="conversation"):
    """A Keeper-shaped ID from a Slack message: redelivery maps to the same one."""
    return f"{int(float(ts)):010d}-" + hashlib.sha256(f"{kind}:{channel}:{ts}".encode()).hexdigest()[:32]


# ------------------------------------------------------------ transport ----
def call(method, token, **payload):
    request = urllib.request.Request(f"{API}/api/{method}", data=json.dumps(payload).encode(), method="POST",
                                     headers={"Authorization": "Bearer " + token,
                                              "Content-Type": "application/json; charset=utf-8"})
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            value = json.loads(response.read(FRAME_LIMIT))
    except (OSError, ValueError) as error:
        raise SlackError(f"{method}: {str(error)[:200]}") from None
    if not isinstance(value, dict) or not value.get("ok"):
        raise SlackError(f"{method}: {value.get('error', 'failed') if isinstance(value, dict) else 'bad reply'}")
    return value


class WebSocket:
    """RFC 6455 client, text frames only: enough for Socket Mode."""

    def __init__(self, url, timeout=IDLE):
        parts = urllib.parse.urlsplit(url)
        if parts.scheme != "wss" and not (TEST and parts.scheme == "ws"):
            raise SlackError("Socket Mode URL must be wss://")
        port = parts.port or (443 if parts.scheme == "wss" else 80)
        sock = socket.create_connection((parts.hostname, port), timeout=timeout)
        if parts.scheme == "wss":
            sock = ssl.create_default_context().wrap_socket(sock, server_hostname=parts.hostname)
        self.sock, self.buffer, self.lock = sock, b"", threading.Lock()
        key = base64.b64encode(os.urandom(16)).decode()
        path = (parts.path or "/") + ("?" + parts.query if parts.query else "")
        sock.sendall((f"GET {path} HTTP/1.1\r\nHost: {parts.hostname}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                      f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n").encode())
        while b"\r\n\r\n" not in self.buffer:
            if len(self.buffer) > 16384:
                raise SlackError("oversized WebSocket handshake")
            self._fill()
        head, _, self.buffer = self.buffer.partition(b"\r\n\r\n")
        lines = head.decode("latin-1").split("\r\n")
        headers = {k.strip().lower(): v.strip() for k, _, v in (line.partition(":") for line in lines[1:])}
        accept = base64.b64encode(hashlib.sha1((key + GUID).encode()).digest()).decode()
        if lines[0].split()[1:2] != ["101"] or headers.get("sec-websocket-accept") != accept:
            raise SlackError("WebSocket handshake refused: " + lines[0][:80])

    def _fill(self):
        chunk = self.sock.recv(65536)
        if not chunk:
            raise ConnectionError("Slack closed the connection")
        self.buffer += chunk

    def _frame(self):
        """One complete frame from the buffer, else None; a timeout never splits one."""
        buf = self.buffer
        if len(buf) < 2:
            return None
        n, at = buf[1] & 0x7F, 2
        if n >= 126:
            size = 2 if n == 126 else 8
            if len(buf) < 2 + size:
                return None
            n, at = int.from_bytes(buf[2:2 + size], "big"), 2 + size
        if n > FRAME_LIMIT:
            raise SlackError("WebSocket frame over 1 MiB")
        mask = buf[at:at + 4] if buf[1] & 0x80 else None
        at += 4 if mask else 0
        if len(buf) < at + n:
            return None
        payload = buf[at:at + n]
        if mask:
            payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        self.buffer = buf[at + n:]
        return bool(buf[0] & 0x80), buf[0] & 0x0F, payload

    def send(self, opcode, payload=b""):
        mask, n = os.urandom(4), len(payload)
        header = bytes([0x80 | opcode]) + (bytes([0x80 | n]) if n < 126 else bytes([0xFE]) + struct.pack(">H", n)
                                           if n < 65536 else bytes([0xFF]) + struct.pack(">Q", n))
        body = (int.from_bytes(payload, "big") ^ int.from_bytes((mask * (n // 4 + 1))[:n], "big")).to_bytes(n, "big")
        with self.lock:
            self.sock.sendall(header + mask + body)

    def recv(self):
        """The next text message; pings answered; None once the server closes."""
        message = None
        while True:
            frame = self._frame()
            while frame is None:
                self._fill()
                frame = self._frame()
            fin, opcode, payload = frame
            if opcode == 9:
                self.send(10, payload)
            elif opcode == 8:
                return None
            elif opcode in (1, 2):
                message = [opcode, payload]
            elif opcode == 0 and message:
                message[1] += payload
                if len(message[1]) > FRAME_LIMIT:
                    raise SlackError("WebSocket message over 1 MiB")
            if fin and message and opcode in (0, 1, 2):
                if message[0] == 1:
                    return message[1].decode("utf-8", "replace")
                message = None

    def close(self):
        try:
            self.send(8)
        except OSError:
            pass
        self.sock.close()


# ---------------------------------------------------------------- intake ----
class Channel:
    """One digger's Slack app. ``runner`` is the DiggerRunner: ``definition``,
    ``slack_ask``, ``record``, ``audit``, ``approvals`` and ``origin``."""

    def __init__(self, runner, tokens, stop):
        self.runner, self.binding, self.stop = runner, runner.definition["slack"], stop
        self.name = runner.definition["name"]
        self.app, self.bot = tokens["SLACK_APP_TOKEN"], tokens["SLACK_BOT_TOKEN"]
        self.state = {"connected": False, "since": time.time(), "error": "connecting"}
        self.bot_user = None
        self.envelopes, self.messages = deque(maxlen=512), deque(maxlen=512)   # Socket Mode redelivers
        self.asked = {}       # approval id -> (channel, ts, tool)
        self.thread = None

    def start(self):
        self.thread = threading.Thread(target=self.run, name="slack", daemon=True)
        self.thread.start()

    def health(self):
        return dict(self.state, approvals=len(self.asked))

    def log(self, text):
        print(f"{self.name}: slack: {str(text)[:300]}", file=sys.stderr, flush=True)

    def run(self):
        delay = 1
        while not self.stop.is_set():
            ws = None
            try:
                if not self.bot_user:
                    self.bot_user = call("auth.test", self.bot)["user_id"]
                ws = WebSocket(call("apps.connections.open", self.app)["url"])
                idle = 0
                while not self.stop.is_set():
                    try:
                        text = ws.recv()
                    except socket.timeout:
                        idle += 1
                        if idle >= 3:
                            raise SlackError(f"no traffic for {3 * IDLE} s")
                        ws.send(9)
                        continue
                    idle = 0
                    if text is None:
                        break
                    envelope = json.loads(text)
                    kind = envelope.get("type")
                    if kind == "hello":
                        self.state, delay = {"connected": True, "since": time.time(), "error": None}, 1
                        continue
                    if kind == "disconnect":
                        break
                    eid = envelope.get("envelope_id")
                    if eid:
                        ws.send(1, json.dumps({"envelope_id": eid}).encode())   # ack before any work
                    payload = envelope.get("payload") or {}
                    keys = {eid, payload.get("event_id")} - {None}
                    if keys & set(self.envelopes):
                        continue
                    self.envelopes.extend(keys)
                    try:
                        if kind == "events_api":
                            self.event(payload.get("event") or {})
                        elif kind == "interactive":
                            self.action(payload)
                    except Exception as error:   # one bad envelope never drops the connection
                        self.log(f"{kind}: {error}")
                self.state = {"connected": False, "since": time.time(), "error": "reconnecting"}
            except (OSError, SlackError, ValueError) as error:
                self.state = {"connected": False, "since": time.time(), "error": str(error)[:200]}
                self.log(error)
                self.stop.wait(delay)
                delay = min(delay * 2, 60)
            finally:
                if ws:
                    ws.close()

    # ---- messages ----
    def post(self, where, text, blocks=None):
        payload = {"channel": where["channel"], "thread_ts": where["thread_ts"], "text": text[:REPLY_LIMIT],
                   "unfurl_links": False}
        if blocks:
            payload["blocks"] = blocks
        try:
            return call("chat.postMessage", self.bot, **payload).get("ts")
        except SlackError as error:
            self.log(error)

    def react(self, channel, ts, name):
        try:
            call("reactions.add", self.bot, channel=channel, timestamp=ts, name=name)
        except SlackError as error:
            if "already_reacted" not in str(error):
                self.log(error)

    def event(self, event):
        if event.get("type") not in ("app_mention", "message") or event.get("subtype") or event.get("bot_id"):
            return   # edits, joins, bot posts: never a request (bots never summon a digger)
        user, channel, ts = event.get("user"), event.get("channel"), event.get("ts")
        if not (isinstance(user, str) and isinstance(channel, str) and isinstance(ts, str)) or user == self.bot_user:
            return
        if (channel, ts) in self.messages:
            return   # the message and app_mention events of one message
        self.messages.append((channel, ts))
        thread = event.get("thread_ts") or ts
        where = {"channel": channel, "thread_ts": thread, "ts": ts, "user": user}
        cid = thread_id(channel, thread)
        text = event.get("text") or ""
        b = self.binding
        if event.get("channel_type") == "im" or channel.startswith("D"):
            if b["dm_policy"] == "deny":
                return
            if user not in b["users"]:
                return self.pair(where) if b["dm_policy"] == "pair" else self.deny(where, cid)
        else:
            if channel not in b["channels"]:
                return   # not bound here: say nothing
            mentioned = event["type"] == "app_mention" or f"<@{self.bot_user}>" in text
            followup = event.get("thread_ts") and self.runner.record(cid) is not None
            if b["require_mention"] and not mentioned and not followup:
                return
            if user not in b["users"]:
                return self.deny(where, cid)
        text = text.replace(f"<@{self.bot_user}>", "").strip()
        if not 0 < len(text) <= MESSAGE_LIMIT:
            self.post(where, f"Ask in 1 to {MESSAGE_LIMIT} characters.")
            return
        self.runner.slack_ask({"id": thread_id(channel, ts, "turn"), "op": "ask", "conversation": cid, "message": text,
                               "requested_by": "slack:" + user, "at": time.time(), "slack": where})

    def deny(self, where, cid):
        self.runner.audit(cid, None, "denied", where)
        self.post(where, "You are not on this digger's allowlist; ask its owner to add you.")

    def pair(self, where):
        code = self.runner.pairing(where["user"])
        self.post(where, f"To use this digger, ask its owner to run: grave digger pair {self.name} {code}"
                         " (valid for an hour).")

    # ---- runner callbacks ----
    def queued(self, request, ahead, paused):
        why = "paused for gaming mode on the box" if paused else f"{ahead} request(s) ahead"
        self.post(request["slack"], f"Queued: {why}. I'll start as soon as I can.")

    def refused(self, request, why):
        self.post(request["slack"], f"Not started: {why}")

    def turn_started(self, request):
        self.react(request["slack"]["channel"], request["slack"]["ts"], "eyes")

    def turn_ended(self, record, text, marker):
        where = record["slack"]
        text = "\n".join(line for line in text.splitlines() if line.strip() != marker).strip()
        if record["state"] != "succeeded":
            text = (text + "\n\n" if text else "") + f":x: The turn {record['state'].replace('_', ' ')}: {record['message'][:300]}"
        if text:
            self.post(where, text)

    def finished(self, record, link):
        where, result = record["slack"], record.get("result") or {}
        verdict, changes = result.get("verdict", "unverified"), result.get("changes") or {}
        checks = result.get("checks") or []
        lines = [f"{':white_check_mark:' if verdict in GOOD else ':x:'} *{verdict}* — {(record.get('summary') or '')[:200]}"]
        if changes.get("commits"):
            lines.append(f"{changes.get('files', 0)} file(s) changed in {changes['commits']} commit(s)")
        if checks:
            lines.append("Checks: " + ", ".join(f"`{c['cmd'][:80]}` {'passed' if c['exit_code'] == 0 else 'failed'}"
                                                for c in checks))
        if record.get("branch") and changes.get("commits"):
            lines.append(f"Branch `{record['branch']}`")
        if link:
            lines.append(f"Review: {link}")
        self.post(where, "\n".join(lines))
        self.react(where["channel"], where["thread_ts"], "white_check_mark" if verdict in GOOD else "x")

    # ---- approvals ----
    def approvals(self, directory):
        """Post each new approval request to its thread; expire the old ones."""
        for path in sorted(Path(directory).glob("*.json")) if Path(directory).is_dir() else ():
            aid, answer = path.stem, path.with_suffix(".answer")
            if answer.exists():
                self.asked.pop(aid, None)
                if time.time() - answer.stat().st_mtime > 3600:
                    path.unlink(missing_ok=True)
                    answer.unlink(missing_ok=True)
                continue
            if not self.state["connected"]:
                self.runner.answer(path, False, "Slack is disconnected, so no approver can answer")
                continue
            try:
                request = json.loads(path.read_text()[:65536])
            except (OSError, ValueError):
                continue
            if aid in self.asked:
                if time.time() - request["at"] > APPROVAL_EXPIRY:
                    channel, ts, tool = self.asked.pop(aid)
                    self.runner.answer(path, False, "expired after 10 minutes without an approver")
                    self.update(channel, ts, f":hourglass: Expired, denied: `{tool}`")
                continue
            record = self.runner.record(request["conversation"])
            if not record or not record.get("slack"):
                self.runner.answer(path, False, "approvals are asked in the conversation's Slack thread; this one has none")
                continue
            tool, detail = request["tool"][:100], json.dumps(request["input"])[:1500]
            text = f"Approve `{tool}`?"
            blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": f"{text}\n```{detail}```"}},
                      {"type": "actions", "elements": [
                          {"type": "button", "text": {"type": "plain_text", "text": "Approve"}, "style": "primary",
                           "action_id": "approve", "value": aid},
                          {"type": "button", "text": {"type": "plain_text", "text": "Deny"}, "style": "danger",
                           "action_id": "deny", "value": aid}]}]
            ts = self.post(record["slack"], text, blocks)
            if ts:
                self.asked[aid] = (record["slack"]["channel"], ts, tool)

    def action(self, payload):
        if payload.get("type") != "block_actions":
            return
        user = (payload.get("user") or {}).get("id")
        for act in payload.get("actions") or []:
            aid = act.get("value")
            if act.get("action_id") not in ("approve", "deny") or aid not in self.asked:
                continue
            if user not in approvers(self.binding):
                self.log(f"approval click from {user}, who is not an approver: ignored")
                continue
            channel, ts, tool = self.asked.pop(aid)
            allow = act["action_id"] == "approve"
            path = Path(self.runner.approvals) / (aid + ".json")
            self.runner.answer(path, allow, f"{'approved' if allow else 'denied'} by Slack user {user}")
            self.update(channel, ts, f"{':white_check_mark: Approved' if allow else ':no_entry: Denied'} by <@{user}>: `{tool}`")

    def update(self, channel, ts, text):
        try:
            call("chat.update", self.bot, channel=channel, ts=ts, text=text, blocks=[])
        except SlackError as error:
            self.log(error)


def new_code():
    return secrets.token_hex(3)


# ----------------------------------------------------- approval bridge ----
APPROVE_TOOL = {"name": "approve", "description": "Ask an allowlisted human in Slack to approve one tool call.",
                "inputSchema": {"type": "object", "properties": {"tool_name": {"type": "string"},
                                                                 "input": {"type": "object"},
                                                                 "tool_use_id": {"type": "string"}},
                                "required": ["tool_name", "input"]}}


def bridge(directory, conversation, poll=0.5):
    """Claude's --permission-prompt-tool: stdio MCP. It writes one request file and
    waits for the loop to write the answer; it never holds a Slack token."""
    directory = Path(directory)
    stdout = sys.stdout

    def reply(request, result=None, error=None):
        if request.get("id") is None:
            return
        body = {"jsonrpc": "2.0", "id": request["id"]}
        body.update({"error": error} if error else {"result": result})
        stdout.write(json.dumps(body) + "\n")
        stdout.flush()

    for raw in sys.stdin:
        try:
            request = json.loads(raw)
        except ValueError:
            continue
        if not isinstance(request, dict):
            continue
        method = request.get("method")
        if method == "initialize":
            reply(request, {"protocolVersion": (request.get("params") or {}).get("protocolVersion", "2024-11-05"),
                            "capabilities": {"tools": {}}, "serverInfo": {"name": "digger", "version": "1"}})
        elif method == "tools/list":
            reply(request, {"tools": [APPROVE_TOOL]})
        elif method == "tools/call":
            params = request.get("params") or {}
            args = params.get("arguments") or {}
            if params.get("name") != "approve" or not isinstance(args.get("tool_name"), str) \
                    or not isinstance(args.get("input"), dict):
                reply(request, {"content": [{"type": "text", "text": "unknown tool or arguments"}], "isError": True})
                continue
            aid = f"{int(time.time())}-{secrets.token_hex(16)}"
            path = directory / (aid + ".json")
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, "w") as stream:
                json.dump({"id": aid, "conversation": conversation, "tool": args["tool_name"][:200],
                           "input": args["input"], "at": time.time()}, stream)
            answer, deadline = None, time.monotonic() + APPROVAL_EXPIRY + 30
            while answer is None and time.monotonic() < deadline:
                try:
                    answer = json.loads(path.with_suffix(".answer").read_text())
                except (OSError, ValueError):
                    time.sleep(poll)
            if not isinstance(answer, dict) or answer.get("behavior") != "allow":
                decision = {"behavior": "deny", "message": (answer or {}).get("message") or "no approver answered"}
            else:
                decision = {"behavior": "allow", "updatedInput": args["input"]}
            reply(request, {"content": [{"type": "text", "text": json.dumps(decision)}]})
        elif method == "ping":
            reply(request, {})
        elif method and not method.startswith("notifications/"):
            reply(request, error={"code": -32601, "message": "Method not found"})
