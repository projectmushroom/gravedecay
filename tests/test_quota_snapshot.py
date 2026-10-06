"""Provider quota snapshots come from the agent's own transcripts, nothing else.

Codex `exec` rollouts carry `rate_limits` events (confirmed on the appliance,
codex-cli 0.160.0); the newest `codex` entry with windows is an account-wide
ChatGPT-plan snapshot labelled by window length. A Claude run that ends on a
usage-limit 429 records the reset time in its transcript. No code may read
Anthropic's OAuth usage endpoint or the Claude credential file.
"""
import json
import os
import pathlib
import re
import subprocess
import tempfile
import unittest

from test_dashboard import load_dashboard

ROOT = pathlib.Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests/fixtures/quota"
# Built from pieces so this file does not trip its own scan.
FORBIDDEN = ("/api/oauth/" + "usage", "$HOME/.claude/" + ".credentials.json", "${HOME}/.claude/" + ".credentials.json",
             "~/.claude/" + ".credentials.json", "HOME}/.claude/" + ".credentials.json", '.claude", ".credentials' + '.json"')
CREDENTIAL_FILE = ".credentials" + ".json"
CODE = ("bin/", "libexec/", "dashboard/", "clients/", "macos/", "scripts/", "raise.sh", "install.sh", "uninstall.sh")


def rollout(name):
    with open(FIXTURES / name) as fh:
        return [json.loads(line) for line in fh if line.strip()]


class QuotaFixtureTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.home = pathlib.Path(self.tmp.name) / "home"
        self.cwd = str(pathlib.Path(self.tmp.name) / "worktree"); os.makedirs(self.cwd)
        self.dash = load_dashboard({"HOME": str(self.home), "GRAVE_ROOT": self.tmp.name, "GRAVEDECAY_ALLOWED_USERS": "owner@example.test"})

    def place(self, name, stamp, directory=None):
        target = self.home / (".codex/sessions/2026/10/06" if name.startswith("rollout") else ".claude/projects/" + re.sub(r"[^A-Za-z0-9]", "-", directory or self.cwd))
        target.mkdir(parents=True, exist_ok=True)
        path = target / (name if name.startswith("rollout") else "session.jsonl")
        path.write_text((FIXTURES / name).read_text().replace("__CWD__", directory or self.cwd))
        os.utime(path, (stamp, stamp))
        return path

    def test_codex_quota_filters_to_the_codex_limit_with_windows_and_labels_by_length(self):
        weekly = [d["payload"]["rate_limits"] for d in rollout("rollout-weekly-only.jsonl") if d["payload"].get("rate_limits")]
        self.assertEqual(self.dash.codex_quota(weekly[-1]), {"provider": "codex", "account_wide": True, "plan": "prolite",
            "windows": [{"window_minutes": 10080, "used_percent": 46.0, "resets_at": 1790757683}]})
        both = [d["payload"]["rate_limits"] for d in rollout("rollout-5h-plus-weekly.jsonl") if d["payload"].get("rate_limits")]
        self.assertEqual([w["window_minutes"] for w in self.dash.codex_quota(both[-1])["windows"]], [300, 10080])
        self.assertEqual(self.dash.codex_quota(both[-1])["plan"], "plus")
        premium = [d["payload"]["rate_limits"] for d in rollout("rollout-trailing-premium.jsonl") if d["payload"].get("rate_limits")]
        self.assertEqual(premium[-1]["limit_id"], "premium")
        self.assertIsNone(self.dash.codex_quota(premium[-1]))
        self.assertIsNone(self.dash.codex_quota({"limit_id": "codex", "primary": None, "secondary": {"used_percent": 1, "window_minutes": 300, "resets_at": 5}}))
        self.assertIsNone(self.dash.codex_quota({"limit_id": "codex", "primary": {"used_percent": 1, "window_minutes": None, "resets_at": 5}}))
        self.assertIsNone(self.dash.codex_quota(None))
        for name in ("rollout-weekly-only.jsonl", "rollout-5h-plus-weekly.jsonl", "rollout-trailing-premium.jsonl"):
            snapshot = self.dash.codex_quota([d["payload"]["rate_limits"] for d in rollout(name) if d["payload"].get("rate_limits") and d["payload"]["rate_limits"]["limit_id"] == "codex"][-1])
            self.assertNotIn("primary", json.dumps(snapshot)); self.assertNotIn("secondary", json.dumps(snapshot))

    def test_rollout_keeps_the_newest_codex_entry_past_a_trailing_premium_one(self):
        path = self.place("rollout-trailing-premium.jsonl", 1_791_000_000)
        cwd, model, usage, quota = self.dash.codex_rollout(str(path))
        self.assertEqual((cwd, model, usage["input_tokens"]), (self.cwd, "gpt-5.3-codex", 4100))
        self.assertEqual(quota["windows"], [{"window_minutes": 10080, "used_percent": 70.0, "resets_at": 1790757683}])

    def test_usage_panel_survives_a_trailing_premium_entry_and_lists_windows_by_length(self):
        now = int(__import__("time").time())
        self.place("rollout-5h-plus-weekly.jsonl", now - 7200)
        self.place("rollout-trailing-premium.jsonl", now - 60)  # newest file ends on `premium`
        limits = self.dash.collect_agent_usage()["codex_limits"]
        self.assertEqual(limits, {"plan": "prolite", "windows": [{"pct": 70.0, "mins": 10080, "resets_at": 1790757683}]})
        self.assertNotIn("primary", json.dumps(limits))
        os.utime(self.home / ".codex/sessions/2026/10/06/rollout-5h-plus-weekly.jsonl", (now, now))
        fresh = load_dashboard({"HOME": str(self.home), "GRAVE_ROOT": self.tmp.name, "GRAVEDECAY_ALLOWED_USERS": "owner@example.test"})
        self.assertEqual([w["mins"] for w in fresh.collect_agent_usage()["codex_limits"]["windows"]], [300, 10080])

    def test_transcript_cost_returns_the_runs_own_newest_snapshot_only(self):
        self.place("rollout-weekly-only.jsonl", 1_791_000_100)
        elsewhere = str(pathlib.Path(self.tmp.name) / "elsewhere"); os.makedirs(elsewhere)
        other = self.place("rollout-trailing-premium.jsonl", 1_791_000_200, directory=elsewhere)
        cost = self.dash.transcript_cost(self.cwd, since=1_790_000_000)
        self.assertEqual(cost["quota"]["windows"], [{"window_minutes": 10080, "used_percent": 46.0, "resets_at": 1790757683}])
        self.assertEqual(set(cost), {"usd", "estimated", "model", "quota"})
        self.assertEqual(self.dash.transcript_cost(elsewhere, since=1_790_000_000)["quota"]["windows"][0]["used_percent"], 70.0)
        other.unlink()
        self.assertIsNone(self.dash.transcript_cost(elsewhere, since=1_790_000_000))
        self.assertNotIn("quota", self.dash.transcript_cost(self.cwd, since=1_792_000_000) or {})  # rollout older than the run

    def test_claude_429_transcript_yields_the_reset_time_unless_the_session_carried_on(self):
        path = self.place("claude-429.jsonl", 1_791_000_000)
        self.assertEqual(self.dash.claude_rate_limit(self.cwd), {"resets_at": 1790200800, "window": "five_hour"})
        self.assertIsNone(self.dash.claude_rate_limit(str(pathlib.Path(self.tmp.name) / "nowhere")))
        lines = path.read_text().splitlines()
        # A priced reply after the refusal means the run did not end on the 429.
        later = json.loads(lines[1]); later.update(uuid="a3", timestamp="2026-10-06T02:00:30.000Z")
        path.write_text("\n".join(lines + [json.dumps(later)]) + "\n")
        self.assertIsNone(self.dash.claude_rate_limit(self.cwd))
        # A refusal without a reset time is not a deferral.
        refused = json.loads(lines[2]); refused["quotaLimits"] = {"status": "rejected"}
        path.write_text("\n".join(lines[:2] + [json.dumps(refused)]) + "\n")
        self.assertIsNone(self.dash.claude_rate_limit(self.cwd))
        # The cost parser ignores the synthetic error message.
        path.write_text("\n".join(lines) + "\n")
        self.assertEqual(self.dash.transcript_cost(self.cwd)["model"], "claude-sonnet-4-5")


class NoUsageEndpointTests(unittest.TestCase):
    def test_no_file_reads_the_oauth_usage_endpoint_or_the_claude_credential_file(self):
        # Issue #230: Claude quota percent has no documented headless source; the OAuth
        # usage endpoint and the credential file are out of bounds. Transcripts only.
        files = subprocess.run(["git", "-C", str(ROOT), "ls-files", "-z"], check=True, capture_output=True).stdout.split(b"\0")
        offenders = []
        for name in files:
            if not name:
                continue
            path = ROOT / name.decode()
            try:
                text = path.read_text(errors="ignore")
            except (OSError, IsADirectoryError):
                continue
            for needle in FORBIDDEN:
                if needle in text:
                    offenders.append(name.decode() + ": " + needle)
            # Platform code may name the credential file to keep it out of a backup, never to read it.
            if name.decode().startswith(CODE):
                for number, line in enumerate(text.splitlines(), 1):
                    if CREDENTIAL_FILE in line and re.search(r"open\(|read_text|json\.load|\bcat\b|\bjq\b|\bsource\b|<\s*\S*credentials", line):
                        offenders.append(name.decode() + ":" + str(number) + ": " + CREDENTIAL_FILE)
        self.assertEqual(offenders, [])


class QuotaContractTests(unittest.TestCase):
    def test_dashboard_labels_the_snapshot_as_account_wide_and_by_window_length(self):
        shell = (ROOT / "dashboard/static/index.html").read_text()
        self.assertIn("data-quota", shell)
        self.assertIn("account-wide snapshot, ChatGPT-plan sign-in only", shell)
        self.assertIn("quotaWindow(w.window_minutes)", shell)
        self.assertNotIn("limits.primary", shell); self.assertNotIn("L.primary", shell); self.assertNotIn("L.secondary", shell)
        docs = (ROOT / "docs/SCHEDULES.md").read_text()
        self.assertIn('"quota"', docs); self.assertIn("window_minutes", docs); self.assertIn("deferred_to", docs)


if __name__ == "__main__":
    unittest.main()
