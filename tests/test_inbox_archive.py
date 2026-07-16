import importlib.util
import json
import os
import pathlib
import subprocess
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
GRAVE = (ROOT / "bin/grave").read_text()
DASH = (ROOT / "dashboard/gravedecay.py").read_text()
NOTIF_DOCS = (ROOT / "docs/NOTIFICATIONS.md").read_text()


def load_dashboard(grave_root):
    old = dict(os.environ)
    os.environ["GRAVE_ROOT"] = str(grave_root)
    try:
        spec = importlib.util.spec_from_file_location(
            "gravedecay_inbox_probe", ROOT / "dashboard/gravedecay.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        os.environ.clear()
        os.environ.update(old)


class InboxContractTests(unittest.TestCase):
    # #119: a dismissed push must be recoverable; a hostile title must not be
    # able to corrupt the store.

    def test_every_delivery_attempt_is_recorded_via_jq(self):
        # jq builds the JSON line — printf-style interpolation would let a
        # session name with quotes/newlines break the encoding.
        self.assertIn("notify_record", GRAVE)
        self.assertIn('jq -nc --arg t "$1" --arg b "$2" --arg l "$3" --arg e "$4"', GRAVE)
        self.assertIn('notify_record "$1" "$2" "$4" "$5"', GRAVE)

    def test_inbox_is_size_capped_and_private(self):
        self.assertIn('tail -n 400 "$inbox"', GRAVE)
        self.assertIn('chmod 600 "$inbox"', GRAVE)
        # recording is best-effort: a full disk must never break delivery
        self.assertIn("} 2>/dev/null || true", GRAVE)
        # doctor owns the privacy invariant afterwards
        self.assertIn('check "notification inbox is private"', GRAVE)

    def test_restricted_viewers_never_see_inbox_or_transcripts(self):
        self.assertIn('"inbox": [], "agent_history": []', DASH)

    def test_docs_document_the_inbox(self):
        self.assertIn("notifications.jsonl", NOTIF_DOCS)
        self.assertIn("Inbox", NOTIF_DOCS)


class ArchiveContractTests(unittest.TestCase):
    # #110: session logs outlive tmux; resume goes back to the recorded dir;
    # both dashboard endpoints allowlist their path components.

    def test_new_records_the_dir_for_resume(self):
        self.assertIn('\'{dir:$dir, created:$created}\' >"$GRAVE_ROOT/agents/$name/meta.json"', GRAVE)
        self.assertIn("jq -r '.dir // empty' \"$GRAVE_ROOT/agents/$name/meta.json\"", GRAVE)

    def test_resume_uses_exact_session_match(self):
        # `tmux has-session -t name` matches by prefix: a live "api2" would
        # make `resume api` claim the session is already running.
        self.assertIn('tmuxa has-session -t "=$name"', GRAVE)

    def test_agent_log_endpoint_allowlists_both_path_components(self):
        self.assertIn('re.fullmatch(r"[A-Za-z0-9_-]{1,50}", name)', DASH)
        self.assertIn(r're.fullmatch(r"session-\d{8}\.log", fname)', DASH)
        self.assertIn('"/api/agent-log"', DASH)

    def test_transcripts_and_resume_are_owner_gated(self):
        # /api/agent-log is a GET, so it needs its own _forbidden() call
        # (POSTs like session-resume are gated wholesale in do_POST).
        gate = DASH.index('"/api/agent-log"')
        self.assertIn("_forbidden", DASH[gate:gate + 400])
        self.assertIn('"/api/session-resume"', DASH)

    def test_dashboard_registers_both_panels(self):
        self.assertIn('data-panel="sessions"', DASH)
        self.assertIn('data-panel="inbox"', DASH)
        self.assertIn("sessions:'work',inbox:'work'", DASH)


class InboxExecutionTests(unittest.TestCase):
    """Drive bin/grave notify with a temp conf and a fake curl on PATH,
    then read the inbox file it must leave behind."""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)

    def run_notify(self, *args, configured=True):
        tmp = pathlib.Path(self.tmpdir.name)
        (tmp / "logs").mkdir(exist_ok=True)
        (tmp / "config/secrets").mkdir(parents=True, exist_ok=True)
        conf = tmp / "grave.conf"
        conf.write_text(f'GRAVE_ROOT="{tmp}"\n')
        if configured:
            (tmp / "config/secrets/notify.env").write_text(
                "NTFY_URL=https://ntfy.example/topic\n")
        bindir = tmp / "bin"
        bindir.mkdir(exist_ok=True)
        (bindir / "curl").write_text("#!/usr/bin/env bash\nexit 0\n")
        (bindir / "curl").chmod(0o755)
        env = dict(os.environ, GRAVE_CONF=str(conf),
                   PATH=f"{bindir}:{os.environ['PATH']}")
        proc = subprocess.run([str(ROOT / "bin/grave"), "notify", *args],
                              env=env, capture_output=True, text=True, timeout=30)
        return proc, tmp / "logs/notifications.jsonl"

    def test_delivered_page_lands_in_the_inbox(self):
        proc, inbox = self.run_notify("--event", "doctor", "--link", "/grave/?tab=system",
                                      "doctor: 1 check(s) failed", "on box")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        entry = json.loads(inbox.read_text().strip())
        self.assertEqual(entry["event"], "doctor")
        self.assertEqual(entry["title"], "doctor: 1 check(s) failed")
        self.assertEqual(entry["link"], "/grave/?tab=system")
        self.assertTrue(entry["delivered"])
        self.assertEqual(inbox.stat().st_mode & 0o777, 0o600)

    def test_hostile_title_cannot_corrupt_the_store(self):
        title = 'session "x\ny", done={"a":1}\\'
        proc, inbox = self.run_notify("--event", "bell", title, "body")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        entry = json.loads(inbox.read_text().strip())  # still one valid line
        self.assertEqual(entry["title"], title)

    def test_muted_or_unconfigured_page_leaves_no_trace(self):
        proc, inbox = self.run_notify("--event", "doctor", "t", "b", configured=False)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertFalse(inbox.exists())


class CollectorExecutionTests(unittest.TestCase):
    """Run the dashboard collectors against a synthetic GRAVE_ROOT."""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.root = pathlib.Path(self.tmpdir.name)

    def test_inbox_newest_first_with_link_fallback(self):
        (self.root / "logs").mkdir()
        lines = [json.dumps({"ts": 100, "event": "bell", "title": "old",
                             "body": "b", "link": "", "delivered": True}),
                 json.dumps({"ts": 200, "event": "digest", "title": "new",
                             "body": "b", "link": "/grave/", "delivered": False}),
                 "not json"]
        (self.root / "logs/notifications.jsonl").write_text("\n".join(lines) + "\n")
        dash = load_dashboard(self.root)
        items = dash.collect_inbox()
        self.assertEqual([i["title"] for i in items], ["new", "old"])
        self.assertEqual(items[1]["link"], "/grave/")   # empty link → dashboard
        self.assertFalse(items[0]["delivered"])

    def test_history_skips_t3code_and_unsafe_names(self):
        for name in ("mybot", "t3code", "evil name", "logless"):
            (self.root / "agents" / name).mkdir(parents=True)
        (self.root / "agents/mybot/session-20260101.log").write_text("x" * 2048)
        (self.root / "agents/mybot/session-20260102.log").write_text("y")
        (self.root / "agents/t3code/session-20260101.log").write_text("z")
        (self.root / "agents/evil name/session-20260101.log").write_text("z")
        dash = load_dashboard(self.root)
        hist = dash.collect_agent_history()
        self.assertEqual([h["name"] for h in hist], ["mybot"])
        self.assertEqual(hist[0]["logs"][0], "session-20260102.log")  # newest first
        self.assertEqual(hist[0]["size_kb"], 2)


if __name__ == "__main__":
    unittest.main()
