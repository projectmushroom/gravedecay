import pathlib
import re
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
GRAVE = (ROOT / "bin/grave").read_text()
RAISE = (ROOT / "raise.sh").read_text()
DASH = (ROOT / "dashboard/gravedecay.py").read_text()
DOCS = (ROOT / "docs/NOTIFICATIONS.md").read_text()
SERVICE = (ROOT / "systemd/gravedecay-digest.service.tmpl").read_text()
TIMER = (ROOT / "systemd/gravedecay-digest.timer.tmpl").read_text()


class DigestContractTests(unittest.TestCase):
    # #106: one morning page summarizing the graveyard shift, without a human
    # asking — and without spamming boxes that never opted into notifications.

    def test_raise_installs_and_enables_the_timer_on_every_host(self):
        self.assertIn('step "Morning digest timer"', RAISE)
        self.assertIn("install_unit gravedecay-digest.service", RAISE)
        self.assertIn("install_unit gravedecay-digest.timer", RAISE)
        self.assertIn("enable_restart gravedecay-digest.timer", RAISE)

    def test_timer_reports_late_rather_than_never(self):
        self.assertIn("Persistent=true", TIMER)
        self.assertIn("WantedBy=timers.target", TIMER)

    def test_delivery_is_gated_like_every_other_event_class(self):
        # The timer fires on EVERY box; silence on unconfigured/muted boxes is
        # cmd_notify's --event gating, which the digest must go through.
        self.assertIn('cmd_notify --event digest --link "/grave/" "$title" "$body"', GRAVE)
        self.assertIn("digest", re.search(
            r': "\$\{NOTIFY_EVENTS:=([^}]*)\}"', GRAVE).group(1).split())

    def test_digest_suppresses_doctors_own_page(self):
        # The digest embeds a full doctor run; without --no-page a contract
        # that broke overnight would page twice at 08:00 — the fast route to
        # the human muting notifications wholesale.
        self.assertIn('"$0" doctor --no-page', GRAVE)
        self.assertIn("(( FAILURES > 0 && ! no_page ))", GRAVE)
        # ...and plain `grave doctor` still pages: the flag must be opt-in.
        self.assertIn('[[ "${1:-}" == "--no-page" ]] && no_page=1', GRAVE)

    def test_spend_comes_from_the_dashboard_not_a_second_parser(self):
        # collect_agent_usage() is the one implementation of transcript
        # parsing; the digest asks it on loopback instead of duplicating it.
        self.assertIn("/api/state", GRAVE)
        self.assertIn("dashboard stopped", GRAVE)  # absence is morning news

    def test_session_counts_cover_t3_not_just_tmux(self):
        # T3 drives the same claude/codex binaries with default state dirs, so
        # sessions are counted from those (one Claude .jsonl / one Codex
        # rollout per session) — a tmux-only count reads "0 agents" every
        # morning on a T3-first box while the spend line shows real money.
        self.assertIn(".claude/projects", GRAVE)
        self.assertIn("rollout-*.jsonl", GRAVE)
        self.assertIn("-not -path '*/subagents/*'", GRAVE)

    def test_digest_failure_pages_via_shared_notifier(self):
        self.assertIn("OnFailure=gravedecay-notify@%n.service", SERVICE)

    def test_doctor_enforces_digest_timer_like_backup(self):
        self.assertIn('check "digest timer enabled"', GRAVE)
        self.assertIn('check "digest timer active"', GRAVE)
        self.assertIn("systemctl cat gravedecay-digest.timer", GRAVE)
        self.assertIn("digest timer not installed — re-run raise.sh", GRAVE)

    def test_dashboard_offers_the_mute_checkbox(self):
        # The ⚙️ notification preferences enumerate NOTIFY_CLASSES; a class
        # without a checkbox could never be muted without editing grave.conf.
        self.assertIn('"digest"', DASH)
        self.assertIn("'digest':'morning digest'", DASH)

    def test_docs_table_documents_the_class(self):
        self.assertIn("| `digest` |", DOCS)
        self.assertIn("grave digest --print", DOCS)


if __name__ == "__main__":
    unittest.main()
