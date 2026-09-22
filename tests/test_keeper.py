"""The Gravekeeper: provider event mapping, turn runner, MCP tools and owner-gated endpoints."""
import http.client
import json
import os
import pathlib
import re
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import uuid
from unittest import mock

import test_dashboard as fixtures

ROOT = fixtures.ROOT
kp = fixtures.DASHBOARD.keeper
GRAVE = (ROOT / "bin/grave").read_text()
RAISE = (ROOT / "raise.sh").read_text()

CLAUDE_LINES = [
    '{"type":"system","subtype":"init","session_id":"sess-1","tools":["mcp__keeper__get_resource"],"model":"claude-haiku-4-5"}',
    '{"type":"assistant","message":{"role":"assistant","content":[{"type":"text","text":"Let me look."},'
    '{"type":"tool_use","id":"toolu_1","name":"mcp__keeper__get_resource","input":{"name":"services"}}]},"session_id":"sess-1"}',
    '{"type":"user","message":{"role":"user","content":[{"type":"tool_result","tool_use_id":"toolu_1",'
    '"content":[{"type":"text","text":"{\\"kind\\":\\"services\\"}"}]}]},"session_id":"sess-1"}',
    '{"type":"assistant","message":{"role":"assistant","content":[{"type":"text","text":"All services are up."}]},"session_id":"sess-1"}',
    '{"type":"result","subtype":"success","is_error":false,"result":"All services are up.","session_id":"sess-1","num_turns":2}',
]
CODEX_LINES = [
    '{"type":"thread.started","thread_id":"thr-1"}',
    '{"type":"turn.started"}',
    '{"type":"item.started","item":{"id":"item_1","type":"mcp_tool_call","server":"keeper","tool":"get_logs",'
    '"arguments":{"target":"dash","lines":20},"status":"in_progress"}}',
    '{"type":"item.completed","item":{"id":"item_1","type":"mcp_tool_call","server":"keeper","tool":"get_logs",'
    '"arguments":{"target":"dash","lines":20},"status":"completed","result":{"content":[{"type":"text","text":"line one\\nline two"}]},"error":null}}',
    '{"type":"item.completed","item":{"id":"item_2","type":"agent_message","text":"The dashboard log is quiet."}}',
    '{"type":"turn.completed","usage":{"input_tokens":1,"output_tokens":1}}',
]
FAKE_PROVIDER = '''
import json, os, sys, time
prompt = sys.stdin.read()
spec = json.load(open(os.environ["FAKE_SPEC"]))
with open(os.environ["FAKE_ARGV"], "a") as f:
    f.write(json.dumps({"argv": sys.argv[1:], "prompt": prompt, "cwd": os.getcwd(), "env": sorted(os.environ)}) + "\\n")
for line in spec["lines"]:
    if isinstance(line, (int, float)):
        time.sleep(line); continue
    sys.stdout.write(line + "\\n"); sys.stdout.flush()
sys.stderr.write(spec.get("stderr", ""))
sys.exit(spec.get("rc", 0))
'''


def new_id(age=0):
    return str(int(time.time()) - age) + "-" + uuid.uuid4().hex


def types(events):
    return [e["type"] for e in events]


class Mapping(unittest.TestCase):
    def map_all(self, mapper, lines):
        events, session = [], None
        for line in lines:
            new, found = mapper(json.loads(line))
            events += new
            session = session or found
        return events, session

    def test_claude_fixture(self):
        events, session = self.map_all(kp.claude_events, CLAUDE_LINES)
        self.assertEqual(session, "sess-1")
        self.assertEqual(types(events), ["text", "tool_call", "tool_result", "text", "done"])
        self.assertEqual(events[1]["name"], "mcp__keeper__get_resource")
        self.assertEqual(json.loads(events[1]["text"]), {"name": "services"})
        self.assertEqual(events[2]["text"], '{"kind":"services"}')

    def test_codex_fixture(self):
        events, session = self.map_all(kp.codex_events, CODEX_LINES)
        self.assertEqual(session, "thr-1")
        self.assertEqual(types(events), ["tool_call", "tool_result", "text", "done"])
        self.assertEqual(events[0]["name"], "keeper__get_logs")
        self.assertEqual(events[1]["text"], "line one\nline two")
        self.assertEqual(events[2]["text"], "The dashboard log is quiet.")

    def test_provider_errors_and_unknown_lines(self):
        error, _ = kp.claude_events({"type": "result", "subtype": "error_during_execution", "is_error": True, "result": "Not logged in"})
        self.assertEqual(error, [{"type": "error", "text": "Not logged in"}])
        self.assertEqual(kp.claude_events({"type": "result", "subtype": "error_max_turns", "is_error": False})[0][0]["type"], "error")
        self.assertEqual(kp.codex_events({"type": "turn.failed", "error": {"message": "quota"}})[0], [{"type": "error", "text": "quota"}])
        self.assertEqual(kp.codex_events({"type": "item.completed", "item": {"type": "error", "message": "boom"}})[0][0]["text"], "boom")
        for obj in ({"type": "weird"}, {"type": "assistant", "message": {"content": "not a list"}},
                    {"type": "item.completed", "item": {"type": "reasoning", "text": "hidden"}}, {}):
            self.assertEqual(kp.claude_events(obj)[0] if "message" in obj else kp.codex_events(obj)[0], [])

    def test_secret_environment_is_scrubbed(self):
        env = {"PATH": "/bin", "HOME": "/h", "GRAVE_ROOT": "/srv/dev", "ANTHROPIC_API_KEY": "x", "LINEAR_TOKEN": "x",
               "NTFY_SECRET": "x", "DB_PASSWORD": "x", "AWS_CREDENTIAL_FILE": "x", "KEY_ID": "x", "KEYBOARD": "us"}
        self.assertEqual(kp.scrub_env(env), {"PATH": "/bin", "HOME": "/h", "GRAVE_ROOT": "/srv/dev", "KEYBOARD": "us"})


class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.temp.name)
        self.fake = self.root / "fake.py"
        self.fake.write_text(FAKE_PROVIDER)
        self.spec, self.argv = self.root / "spec.json", self.root / "argv.jsonl"
        self.env = mock.patch.dict(os.environ, {"FAKE_SPEC": str(self.spec), "FAKE_ARGV": str(self.argv), "KEEPER_TEST_TOKEN": "hidden"})
        self.env.start()
        self.runner = kp.Runner(self.root / "keeper", "testhost", script=str(ROOT / "dashboard/keeper.py"), grave="/bin/true",
                                api="http://127.0.0.1:1/grave", work_dir=str(self.root), timeout=3)
        self.runner.command = lambda record: [sys.executable, str(self.fake), record["provider"], record["session_id"] or ""]
        self.provide(CLAUDE_LINES)

    def tearDown(self):
        deadline = time.monotonic() + 5
        while self.runner.processes and time.monotonic() < deadline:
            time.sleep(.01)
        self.env.stop()
        self.temp.cleanup()

    def provide(self, lines, **extra):
        self.spec.write_text(json.dumps({"lines": lines, **extra}))

    def launches(self):
        return [json.loads(line) for line in self.argv.read_text().splitlines()] if self.argv.exists() else []

    def finish(self, conversation, owner="owner@example.test"):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            result = self.runner.get(conversation, owner)
            if result["state"] != "running" and conversation not in self.runner.processes:
                return result
            time.sleep(.02)
        self.fail("turn did not finish")

    def test_turn_persists_before_launch_resumes_session_and_never_resends(self):
        conversation, turn = new_id(), new_id()
        record, created = self.runner.create(conversation, "owner@example.test")
        self.assertTrue(created)
        self.assertEqual((record["state"], record["host"], record["provider"]), ("idle", "testhost", "claude"))
        self.assertFalse(self.runner.create(conversation, "owner@example.test")[1])
        result, created = self.runner.send(conversation, turn, "What's wrong?", "owner@example.test")
        self.assertTrue(created)
        self.assertEqual(result["turn"]["id"], turn)
        self.assertEqual((self.root / "keeper" / (conversation + ".json")).stat().st_mode & 0o777, 0o600)
        result = self.finish(conversation)
        self.assertEqual(result["state"], "succeeded")
        self.assertEqual(result["session_id"], "sess-1")
        self.assertEqual(result["summary"], "All services are up.")
        self.assertEqual(types(result["events"]), ["text", "text", "tool_call", "tool_result", "text", "done"])
        self.assertEqual((result["events"][0]["name"], result["events"][0]["text"]), ("owner", "What's wrong?"))
        self.assertEqual([e["name"] for e in result["events"][2:4]], ["mcp__keeper__get_resource"] * 2)
        self.assertEqual(self.runner.get(conversation, "owner@example.test", result["cursor"])["events"], [])
        self.assertNotIn("instance", result)
        self.assertFalse(self.runner.send(conversation, turn, "What's wrong?", "owner@example.test")[1])
        self.assertEqual(len(self.launches()), 1)
        launch = self.launches()[0]
        self.assertEqual((launch["prompt"], launch["argv"][1], os.path.realpath(launch["cwd"])), ("What's wrong?", "", os.path.realpath(self.root)))
        self.assertNotIn("KEEPER_TEST_TOKEN", launch["env"])
        self.runner.send(conversation, new_id(), "And now?", "owner@example.test")
        self.finish(conversation)
        self.assertEqual(self.launches()[1]["argv"][1], "sess-1")
        listed = self.runner.list("owner@example.test")
        self.assertEqual([row["id"] for row in listed], [conversation])
        self.assertEqual(self.runner.list("someone@example.test"), [])

    def test_one_turn_at_a_time_and_cancellation(self):
        self.provide([CLAUDE_LINES[0], 30])
        conversation = new_id()
        self.runner.create(conversation, "o")
        self.runner.send(conversation, new_id(), "slow", "o")
        with self.assertRaises(kp.KeeperError) as error:
            self.runner.send(conversation, new_id(), "again", "o")
        self.assertEqual(error.exception.code, "busy")
        deadline = time.monotonic() + 3
        while not (self.runner.processes.get(conversation) and self.argv.exists()) and time.monotonic() < deadline:
            time.sleep(.01)
        self.runner.cancel(conversation, "o")
        result = self.finish(conversation, "o")
        self.assertEqual((result["state"], result["message"]), ("cancelled", "Cancelled by the owner"))
        self.assertEqual(len(self.launches()), 1)
        self.provide(CLAUDE_LINES)
        self.runner.send(conversation, new_id(), "next", "o")
        self.assertEqual(self.finish(conversation, "o")["state"], "succeeded")

    def test_timeout_failure_and_malformed_output(self):
        self.runner.timeout = .3
        self.provide([CLAUDE_LINES[0], 30])
        conversation = new_id()
        self.runner.create(conversation, "o")
        self.runner.send(conversation, new_id(), "hang", "o")
        self.assertEqual(self.finish(conversation, "o")["state"], "timed_out")
        self.runner.timeout = 3
        self.provide(["not json", '["a list"]', CLAUDE_LINES[0], '{"type":"assistant","message":{"content":[{"type":"text","text":"partial"}]}}'],
                     rc=2, stderr="Not logged in. Please run /login\n")
        self.runner.send(conversation, new_id(), "broken", "o")
        result = self.finish(conversation, "o")
        self.assertEqual(result["state"], "failed")
        self.assertIn("Not logged in", result["message"])
        self.assertEqual(types(result["events"])[-1], "text")
        self.provide([CLAUDE_LINES[0], '{"type":"result","subtype":"error_during_execution","is_error":true,"result":"Rate limited"}'])
        self.runner.send(conversation, new_id(), "again", "o")
        result = self.finish(conversation, "o")
        self.assertEqual((result["state"], result["message"]), ("failed", "Rate limited"))

    def test_restart_marks_running_turns_interrupted_without_rerun(self):
        conversation = new_id()
        self.runner.create(conversation, "o")
        self.runner.send(conversation, new_id(), "hi", "o")
        self.finish(conversation, "o")
        path = self.root / "keeper" / (conversation + ".json")
        record = json.loads(path.read_text())
        record.update(state="running", instance="gone")
        path.write_text(json.dumps(record))
        other = kp.Runner(self.root / "keeper", "testhost", work_dir=str(self.root))
        result = other.get(conversation, "o")
        self.assertEqual(result["state"], "interrupted")
        self.assertIn("outcome unknown", result["message"])
        self.assertEqual(len(self.launches()), 1)
        self.assertEqual(other.list("o")[0]["state"], "interrupted")
        self.assertTrue(other.health()["ok"])

    def test_bounds_and_owner_binding(self):
        conversation = new_id()
        self.runner.create(conversation, "o")
        for owner, turn, message, code in (("stranger", new_id(), "hi", "not_found"), ("o", "bad", "hi", "invalid_id"),
                                           ("o", new_id(), "", "invalid_message"), ("o", new_id(), "x" * 8001, "invalid_message"),
                                           ("o", new_id(2 * kp.KEY_AGE), "hi", "expired_id")):
            with self.assertRaises(kp.KeeperError) as error:
                self.runner.send(conversation, turn, message, owner)
            self.assertEqual(error.exception.code, code)
        with self.assertRaises(kp.KeeperError):
            self.runner.get(conversation, "stranger")
        with self.assertRaises(kp.KeeperError) as error:
            self.runner.create(new_id(), "o", "gemini")
        self.assertEqual(error.exception.code, "invalid_provider")
        with self.assertRaises(kp.KeeperError) as error:
            self.runner.create(new_id(2 * kp.KEY_AGE), "o")
        self.assertEqual(error.exception.code, "expired_id")
        self.provide([CLAUDE_LINES[0], '{"type":"assistant","message":{"content":[{"type":"text","text":"' + "x" * 40000 + '"}]}}'] * 4
                     + [CLAUDE_LINES[-1]])
        self.runner.send(conversation, new_id(), "flood", "o")
        result = self.finish(conversation, "o")
        self.assertEqual(result["state"], "succeeded")
        self.assertTrue(all(len(e["text"].encode()) <= kp.MAX_TEXT for e in result["events"]))
        self.assertEqual(len(self.launches()), 1)
        self.assertEqual(self.runner.health()["records"], 1)

    def test_installed_cli_commands(self):
        runner = kp.Runner("/tmp/x", "h", script="/srv/dev/scripts/keeper.py", grave="/usr/local/bin/grave",
                           api="http://127.0.0.1:4712/grave", work_dir="/srv/dev/config/keeper")
        claude = runner.command({"provider": "claude", "model": "", "session_id": None})
        self.assertEqual(claude[:5], ["claude", "-p", "--output-format", "stream-json", "--verbose"])
        config = json.loads(claude[claude.index("--mcp-config") + 1])
        self.assertEqual(set(config["mcpServers"]), {"keeper"})
        self.assertEqual(config["mcpServers"]["keeper"]["args"][:2], ["/srv/dev/scripts/keeper.py", "mcp"])
        self.assertEqual(claude[claude.index("--allowedTools") + 1], "mcp__keeper")
        self.assertNotIn("--resume", claude)
        self.assertNotIn("--dangerously-skip-permissions", claude)
        resumed = runner.command({"provider": "claude", "model": "haiku", "session_id": "sess-1"})
        self.assertEqual(resumed[-4:], ["--model", "haiku", "--resume", "sess-1"])
        codex = runner.command({"provider": "codex", "model": "", "session_id": None})
        self.assertEqual(codex[:4], ["codex", "exec", "-C", "/srv/dev/config/keeper"])
        self.assertEqual(codex[-1], "-")
        self.assertIn('sandbox_mode="read-only"', codex)
        self.assertIn('approval_policy="never"', codex)
        self.assertIn('mcp_servers.keeper.default_tools_approval_mode="approve"', codex)
        self.assertNotIn("--dangerously-bypass-approvals-and-sandbox", codex)
        resumed = runner.command({"provider": "codex", "model": "gpt-5.5", "session_id": "thr-1"})
        self.assertEqual(resumed[:4], ["codex", "exec", "resume", "thr-1"])
        self.assertNotIn("-C", resumed)
        self.assertEqual(resumed[-3:], ["-m", "gpt-5.5", "-"])


class KeeperAPI(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = tempfile.TemporaryDirectory()
        root = pathlib.Path(cls.root.name)
        cls.grave = root / "grave"
        cls.grave.write_text('#!/bin/sh\necho "grave $*"\n')
        cls.grave.chmod(0o755)
        cls.d = fixtures.load_dashboard({"GRAVE_ROOT": cls.root.name, "GRAVEDECAY_PLATFORM": "linux",
                                         "GRAVEDECAY_ALLOWED_USERS": "owner@example.test", "GRAVEDECAY_GRAVE": str(cls.grave)})
        cls.server = cls.d.ThreadingHTTPServer(("127.0.0.1", 0), cls.d.Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.api = f"http://127.0.0.1:{cls.server.server_port}/grave"
        os.makedirs(cls.d.KEEPER.work_dir)
        secrets = root / "config" / "secrets"
        secrets.mkdir(parents=True, mode=0o700)
        cls.token = os.urandom(32).hex()
        token = secrets / "dashboard-local-token"
        token.write_text(cls.token + "\n")
        token.chmod(0o600)
        cls.fake = root / "fake.py"
        cls.fake.write_text(FAKE_PROVIDER)
        cls.spec, cls.argv = root / "spec.json", root / "argv.jsonl"
        cls.spec.write_text(json.dumps({"lines": CODEX_LINES}))
        cls.env = mock.patch.dict(os.environ, {"FAKE_SPEC": str(cls.spec), "FAKE_ARGV": str(cls.argv)})
        cls.env.start()
        cls.d.KEEPER.command = lambda record: [sys.executable, str(cls.fake), record["provider"], record["session_id"] or ""]

    @classmethod
    def tearDownClass(cls):
        cls.env.stop()
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()
        cls.root.cleanup()

    def request(self, path, method="GET", data=None, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=10)
        request_headers = {"Tailscale-User-Login": "owner@example.test", "X-Grave-Client": "1"}
        request_headers.update(headers or {})
        request_headers = {k: v for k, v in request_headers.items() if v is not None}
        body = json.dumps(data) if data is not None else None
        if body is not None:
            request_headers["Content-Type"] = "application/json"
        try:
            connection.request(method, path, body, request_headers)
            response = connection.getresponse()
            payload = response.read()
            return response.status, json.loads(payload) if payload else None
        finally:
            connection.close()

    def poll(self, conversation, headers=None):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            code, result = self.request(f"/grave/api/v1/keeper?id={conversation}", headers=headers)
            self.assertEqual(code, 200)
            if result["state"] != "running":
                return result
            time.sleep(.05)
        self.fail("turn did not finish")

    def test_conversation_lifecycle_through_the_owner_api(self):
        code, capabilities = self.request("/grave/api/v1/capabilities")
        self.assertIn("keeper", capabilities["routes"]["GET"])
        self.assertEqual(set(capabilities["routes"]["POST"]) & {"keeper", "keeper/turn", "keeper/cancel"}, {"keeper", "keeper/turn", "keeper/cancel"})
        self.assertEqual(capabilities["keeper"]["tools"], ["get_resource", "get_logs", "run_doctor"])
        code, listing = self.request("/grave/api/v1/keeper")
        self.assertEqual((code, listing["conversations"]), (200, []))
        conversation, turn = new_id(), new_id()
        self.assertEqual(self.request("/grave/api/v1/keeper", "POST", {"id": conversation, "provider": "codex"})[0], 202)
        self.assertEqual(self.request("/grave/api/v1/keeper", "POST", {"id": conversation})[0], 200)
        self.assertEqual(self.request("/grave/api/v1/keeper", "POST", {"id": conversation, "extra": 1})[0], 400)
        code, result = self.request("/grave/api/v1/keeper/turn", "POST", {"conversation": conversation, "turn": turn, "message": "Anything to update?"})
        self.assertEqual((code, result["state"]), (202, "running"))
        self.assertEqual(self.request("/grave/api/v1/keeper/turn", "POST", {"conversation": conversation, "turn": turn, "message": "Anything to update?"})[0], 200)
        result = self.poll(conversation)
        self.assertEqual((result["state"], result["session_id"], result["provider"]), ("succeeded", "thr-1", "codex"))
        self.assertEqual(types(result["events"]), ["text", "tool_call", "tool_result", "text", "done"])
        code, tail = self.request(f"/grave/api/v1/keeper?id={conversation}&after={result['cursor'] - 1}")
        self.assertEqual([e["type"] for e in tail["events"]], ["done"])
        self.assertEqual(self.request(f"/grave/api/v1/keeper?id={conversation}&after=x")[0], 400)
        self.assertEqual(self.request("/grave/api/v1/keeper/cancel", "POST", {"conversation": conversation})[0], 200)
        self.assertEqual(self.request("/grave/api/v1/keeper/cancel", "POST", {"conversation": new_id()})[0], 404)
        code, listing = self.request("/grave/api/v1/keeper")
        self.assertEqual([row["id"] for row in listing["conversations"]], [conversation])
        self.assertEqual(len(self.argv.read_text().splitlines()), 1)

    def test_gates_and_unsupported_deployments(self):
        for headers in ({"X-Grave-Client": None}, {"Tailscale-User-Login": "outsider@example.test"},
                        {"Origin": "https://evil.example"}):
            self.assertIn(self.request("/grave/api/v1/keeper", headers=headers)[0], (403, 404))
        self.assertEqual(self.request("/grave/api/keeper")[0], 404)
        with mock.patch.object(self.d, "MACOS", True):
            self.assertEqual(self.request("/grave/api/v1/keeper")[0], 404)
        with mock.patch.object(self.d, "PORTABLE", True):
            self.assertIn(self.request("/grave/api/v1/keeper")[0], (401, 501))
        with mock.patch.object(self.d.KEEPER, "send", side_effect=OSError("disk")):
            self.assertEqual(self.request("/grave/api/v1/keeper/turn", "POST", {"conversation": new_id(), "turn": new_id(), "message": "x"})[0], 503)

    def test_mcp_server_serves_the_three_tools_through_validated_handlers(self):
        process = subprocess.Popen([sys.executable, str(ROOT / "dashboard/keeper.py"), "mcp", "--root", self.root.name,
                                    "--api", self.api, "--grave", str(self.grave)],
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

        def call(method, params=None, request_id=1):
            process.stdin.write(json.dumps({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params or {}}) + "\n")
            process.stdin.flush()
            return json.loads(process.stdout.readline())

        try:
            self.assertEqual(call("initialize", {"protocolVersion": "2025-06-18"})["result"]["serverInfo"]["name"], "keeper")
            process.stdin.write('{"jsonrpc":"2.0","method":"notifications/initialized"}\n')
            self.assertEqual([t["name"] for t in call("tools/list")["result"]["tools"]], ["get_resource", "get_logs", "run_doctor"])
            result = call("tools/call", {"name": "get_resource", "arguments": {"name": "preferences"}})["result"]
            self.assertFalse(result["isError"])
            self.assertEqual(json.loads(result["content"][0]["text"])["kind"], "preferences")
            result = call("tools/call", {"name": "get_logs", "arguments": {"target": "dash", "lines": 20}})["result"]
            self.assertEqual(result["content"][0]["text"].strip(), "grave logs --tail 20 dash")
            result = call("tools/call", {"name": "run_doctor", "arguments": {}})["result"]
            self.assertFalse(result["isError"])
            self.assertRegex(result["content"][0]["text"], r"^operation [0-9]{10}-[0-9a-f]{32}: succeeded \(exit 0\)\ngrave doctor")
            for arguments in ({"name": "secrets"}, {"target": "agent", "lines": 5}, {"target": "dash", "lines": 5000}, {"target": "dash", "lines": "5"}):
                name = "get_resource" if "name" in arguments else "get_logs"
                self.assertTrue(call("tools/call", {"name": name, "arguments": arguments})["result"]["isError"])
            self.assertTrue(call("tools/call", {"name": "shell", "arguments": {"cmd": "ls"}})["result"]["isError"])
            self.assertEqual(call("resources/list")["error"]["code"], -32601)
        finally:
            process.stdin.close()
            process.wait(timeout=5)


class Contracts(unittest.TestCase):
    def test_doctor_raise_and_cli_wire_the_keeper(self):
        self.assertIn('check "keeper runner installed" test -r "$GRAVE_ROOT/scripts/keeper.py"', GRAVE)
        self.assertIn('check "keeper instructions and private storage"', GRAVE)
        self.assertIn('check "keeper provider installed and logged in"', GRAVE)
        self.assertIn('keeper)     shift || true; exec env GRAVE_ROOT="$GRAVE_ROOT" GRAVE_CONF="$CONF" python3 "$GRAVE_ROOT/scripts/keeper.py" "$@" ;;', GRAVE)
        self.assertIn('install -m 755 "$REPO_DIR/dashboard/keeper.py" "$GRAVE_ROOT/scripts/keeper.py"', RAISE)
        self.assertIn('cp -n "$REPO_DIR/config/GRAVEKEEPER.md" "$GRAVE_ROOT/config/GRAVEKEEPER.md"', RAISE)
        self.assertIn('ln -sfn ../GRAVEKEEPER.md "$GRAVE_ROOT/config/keeper/CLAUDE.md"', RAISE)
        self.assertIn('ln -sfn ../GRAVEKEEPER.md "$GRAVE_ROOT/config/keeper/AGENTS.md"', RAISE)
        self.assertTrue((ROOT / "config/GRAVEKEEPER.md").exists())
        self.assertIn('KEEPER_PROVIDER="claude"', (ROOT / "config/grave.conf.example").read_text())
        for doc, needle in (("API.md", "keeper/turn"), ("SECURITY.md", "## The Gravekeeper"), ("NOTIFICATIONS.md", "Gravekeeper turn")):
            self.assertIn(needle, (ROOT / "docs" / doc).read_text())
        spec = json.loads((ROOT / "docs/openapi.json").read_text())
        self.assertEqual({p for p in spec["paths"] if "keeper" in p}, {"/keeper", "/keeper/turn", "/keeper/cancel"})

    def test_logs_tail_mode_reuses_the_follow_allowlist(self):
        function = re.search(r"^cmd_logs\(\) \{.*?^\}", GRAVE, re.S | re.M).group(0)
        stubs = 'sudo() { echo "sudo $*"; }\ntail() { echo "tail $*"; }\ndk() { return 1; }\nbad() { echo "$*" >&2; }\nLOG_FILE=/var/log/grave.log; GRAVE_ROOT=/srv/dev\n'
        def run(*args):
            return subprocess.run(["bash", "-eu", "-c", stubs + function + '\ncmd_logs "$@"', "logs", *args], capture_output=True, text=True)
        self.assertEqual(run("--tail", "20", "dash").stdout.strip(), "sudo journalctl -u gravedecay -n 20")
        self.assertEqual(run("--tail", "7", "grave").stdout.strip(), "tail -n 7 /var/log/grave.log")
        self.assertEqual(run("--tail", "5", "system").stdout.strip(), "sudo journalctl -p warning -n 5")
        self.assertEqual(run("dash").stdout.strip(), "sudo journalctl -u gravedecay -n 100 -f")
        self.assertEqual(run("t3").stdout.strip(), "sudo journalctl -u t3code -n 100 -f")
        self.assertNotEqual(run("--tail", "many", "dash").returncode, 0)
        self.assertNotEqual(run("--tail", "10; ls", "dash").returncode, 0)

    def test_keeper_check_reports_missing_files_and_provider(self):
        with tempfile.TemporaryDirectory() as directory:
            env = {**os.environ, "GRAVE_ROOT": directory, "GRAVE_CONF": os.path.join(directory, "grave.conf"), "PATH": "/nonexistent"}
            result = subprocess.run([sys.executable, str(ROOT / "dashboard/keeper.py"), "check", "files"], env=env, capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("GRAVEKEEPER.md missing", result.stderr)
            pathlib.Path(directory, "config", "keeper").mkdir(parents=True)
            pathlib.Path(directory, "config", "GRAVEKEEPER.md").write_text("# keeper\n")
            for name in ("CLAUDE.md", "AGENTS.md"):
                os.symlink("../GRAVEKEEPER.md", os.path.join(directory, "config", "keeper", name))
            self.assertEqual(subprocess.run([sys.executable, str(ROOT / "dashboard/keeper.py"), "check", "files"], env=env, capture_output=True, text=True).returncode, 0)
            pathlib.Path(directory, "grave.conf").write_text('KEEPER_PROVIDER="codex"\nKEEPER_MODEL="gpt-5.5"\n')
            result = subprocess.run([sys.executable, str(ROOT / "dashboard/keeper.py"), "check", "provider"], env=env, capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("codex is not installed", result.stderr)


if __name__ == "__main__":
    unittest.main()
