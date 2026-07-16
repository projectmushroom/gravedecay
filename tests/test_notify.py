import os
import pathlib
import re
import subprocess
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
GRAVE = (ROOT / "bin/grave").read_text()
RAISE = (ROOT / "raise.sh").read_text()
TMUX = (ROOT / "config/tmux.conf").read_text()
CONF = (ROOT / "config/grave.conf.example").read_text()
NOTIFY_UNIT = (ROOT / "systemd/gravedecay-notify@.service.tmpl").read_text()
AGENT_NOTIFY = (ROOT / "bin/grave-agent-notify").read_text()

# Every non-instanced platform unit must page on failure. Workspace @-units are
# a deliberate follow-up (they run as grave-<slug>, not the owner).
PLATFORM_UNITS = [
    "gravedecay",
    "t3code",
    "gravedecay-term",
    "gravedecay-gateway",
    "gravedecay-upgrade",
    "gravedecay-upgrade@",
    "gravedecay-gamewatch",
    "gravedecay-keepalive",
    "gravedecay-selfheal",
]


class NotifyContractTests(unittest.TestCase):
    def test_event_path_is_a_silent_noop_when_unconfigured(self):
        # Hooks and OnFailure= fire on every box; an unconfigured channel or a
        # muted event class must never fail (or spam) the caller.
        self.assertIn("notify_configured || exit 0", GRAVE)
        self.assertIn('for e in $NOTIFY_EVENTS; do [[ "$e" == "$event" ]] && enabled=1; done', GRAVE)

    def test_notify_events_defaults_for_pre_feature_confs(self):
        # A grave.conf installed before the feature has no NOTIFY_EVENTS; grave
        # must default it (same pattern as PREVIEW_RANGE) and the example conf
        # must document it.
        self.assertIn(': "${NOTIFY_EVENTS:=session-exit bell agent-done unit-failure doctor}"', GRAVE)
        self.assertIn('NOTIFY_EVENTS="session-exit bell agent-done unit-failure doctor"', CONF)

    def test_title_and_token_cannot_inject_headers(self):
        # A tmux session name reaches curl -H "Title: ..."; a CR/LF in it would
        # smuggle extra headers into the request.
        self.assertIn("title=${title//[$'\\r\\n']/ }", GRAVE)
        self.assertIn("${NTFY_TOKEN//[$'\\r\\n']/}", GRAVE)

    def test_body_uses_data_raw_never_at_file_expansion(self):
        # curl -d/--data-binary read local files when the value starts with @ —
        # a hostile session name must not be able to exfiltrate one.
        self.assertIn('--data-raw "$body"', GRAVE)

    def test_ntfy_url_must_be_http_or_https(self):
        self.assertIn('[[ "${NTFY_URL:-}" =~ ^https?:// ]]', GRAVE)

    def test_tmux_hooks_shell_quote_session_names(self):
        # Sessions on the shared socket are not all charset-validated (T3 and
        # webterm create their own), so interpolating a bare name into the
        # run-shell sh string is an injection. #{q:...} shell-quotes it.
        self.assertIn("session-closed", TMUX)
        self.assertIn("alert-bell", TMUX)
        self.assertIn("#{q:hook_session_name}", TMUX)
        self.assertIn("#{q:session_name}", TMUX)

    def test_platform_units_page_on_failure(self):
        for unit in PLATFORM_UNITS:
            text = (ROOT / f"systemd/{unit}.service.tmpl").read_text()
            self.assertIn(
                "OnFailure=gravedecay-notify@%n.service",
                text,
                f"{unit}.service.tmpl must reference the failure notifier",
            )

    def test_notifier_unit_runs_grave_as_the_owner(self):
        self.assertIn("Type=oneshot", NOTIFY_UNIT)
        self.assertIn("User=@USER@", NOTIFY_UNIT)
        self.assertIn("@GRAVE_BIN@ notify --event unit-failure", NOTIFY_UNIT)

    def test_raise_installs_the_notifier_template(self):
        # Every platform unit references gravedecay-notify@ via OnFailure=, so
        # raise.sh must render it on every host, not behind a profile gate.
        self.assertIn("gravedecay-notify@.service.tmpl", RAISE)
        self.assertIn("install_unit gravedecay-notify@.service", RAISE)

    def test_raise_installs_and_provisions_agent_cli_hooks(self):
        self.assertIn('install -m 755 "$REPO_DIR/bin/grave-agent-notify" "$GRAVE_AGENT_NOTIFY"', RAISE)
        self.assertIn('provision_agent_hooks "$HOME_DIR" "$GRAVE_AGENT_NOTIFY"', RAISE)
        self.assertIn(".hooks.Stop", RAISE)
        self.assertIn(".hooks.Notification", RAISE)
        self.assertIn('notify = ["%s", "codex"]', RAISE)
        self.assertIn("Codex notify already set", RAISE)

    def test_fresh_claude_settings_jq_expression_compiles(self):
        match = re.search(r'jq --arg cmd "\$helper claude" -n \\\n\s+\'([^\']+)\'', RAISE)
        self.assertIsNotNone(match)
        proc = subprocess.run(
            ["jq", "--arg", "cmd", "/tmp/grave-agent-notify claude", "-n", match.group(1)],
            capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_doctor_checks_gate_on_each_channel_existing(self):
        # No notify.env / no enrolled devices → no checks (opt-in); with them,
        # doctor must verify ntfy reachability without publishing (?poll=1),
        # secret modes, the dashboard's push crypto, and the notifier unit.
        self.assertIn('if [[ -e "$NOTIFY_ENV" ]]; then', GRAVE)
        self.assertIn('check "ntfy channel reachable"      notify_reachable', GRAVE)
        self.assertIn('"$NTFY_URL/json?poll=1"', GRAVE)
        self.assertIn("if push_ready; then", GRAVE)
        self.assertIn('check "web push sender ready"', GRAVE)
        self.assertIn("/api/push-key' | jq -e .ok", GRAVE)
        self.assertIn("systemctl cat 'gravedecay-notify@.service'", GRAVE)
        self.assertIn('check "agent notify helper installed"', GRAVE)
        self.assertIn('check "Claude notify hooks installed"', GRAVE)
        self.assertIn('check "Codex notify hook installed"', GRAVE)

    def test_push_leg_builds_json_with_jq_and_respects_delivery_status(self):
        # A hostile session name must not break out of the push payload (jq
        # builds it), and zero-device delivery must read as failure (the
        # dashboard answers non-2xx, surfaced by curl -f).
        self.assertIn('jq -n --arg t "$1" --arg b "$2" --arg p "$3" --arg u "$4" --arg e "$5"', GRAVE)
        self.assertIn('/api/push-send', GRAVE)
        self.assertIn("jq -e '.subscriptions | length > 0'", GRAVE)

    def test_dashboard_event_override_wins_over_conf(self):
        # The ⚙️ checkboxes write config/notify-events (no sudo needed, like
        # the gamewatch flag); grave must prefer it over grave.conf.
        self.assertIn('[[ -r "$GRAVE_ROOT/config/notify-events" ]] && NOTIFY_EVENTS="$(<"$GRAVE_ROOT/config/notify-events")"', GRAVE)

    def test_units_run_the_same_python_that_gets_the_crypto(self):
        # Regression #92: units hardcoded /usr/bin/python3 while raise's
        # cryptography probe used the PATH python (brew on SteamOS) — the module
        # landed in an interpreter the dashboard never runs and the 🔔 enable
        # button stayed dead. One PYTHON_BIN resolution must feed both sides.
        self.assertIn('PYTHON_BIN="$(command -v python3 2>/dev/null || echo /usr/bin/python3)"', RAISE)
        self.assertIn('"$PYTHON_BIN" -c \'import cryptography\'', RAISE)
        self.assertIn('"$PYTHON_BIN" -m pip install', RAISE)
        self.assertIn('s|@PYTHON@|$PYTHON_BIN|g', RAISE)
        for tmpl in ("gravedecay", "gravedecay-gateway", "gravedecay-dashboard@"):
            text = (ROOT / f"systemd/{tmpl}.service.tmpl").read_text()
            self.assertIn("ExecStart=@PYTHON@", text, tmpl)
            self.assertNotIn("/usr/bin/python3", text, tmpl)

    def test_raise_installs_python_cryptography_everywhere(self):
        # Web Push needs the cryptography module on every distro path; the
        # managed-toolchain fallback must stay best-effort (never blocks raise).
        self.assertIn("python-cryptography", RAISE)          # pacman
        self.assertEqual(RAISE.count("python3-cryptography"), 2)  # apt + dnf
        self.assertIn("--user --break-system-packages cryptography", RAISE)

    def test_doctor_failure_pages_without_breaking_doctor(self):
        # The page runs in a subshell (cmd_notify exits) and never changes
        # doctor's own verdict.
        self.assertIn('( cmd_notify --event doctor --priority high', GRAVE)

    def test_agent_notify_adapter_maps_cli_payloads(self):
        self.assertIn("agent-done", AGENT_NOTIFY)
        self.assertIn("hook_event_name", AGENT_NOTIFY)
        self.assertIn("*turn*", AGENT_NOTIFY)
        self.assertIn("GRAVEDECAY_GRAVE", AGENT_NOTIFY)


class NotifyExecutionTests(unittest.TestCase):
    """Drive bin/grave notify with a temp conf and a fake curl on PATH."""

    def run_notify(self, *args, notify_env=None, events="session-exit bell agent-done unit-failure doctor"):
        tmp = pathlib.Path(self.tmpdir.name)
        (tmp / "logs").mkdir(exist_ok=True)
        (tmp / "config/secrets").mkdir(parents=True, exist_ok=True)
        conf = tmp / "grave.conf"
        conf.write_text(f'GRAVE_ROOT="{tmp}"\nNOTIFY_EVENTS="{events}"\n')
        if notify_env is not None:
            (tmp / "config/secrets/notify.env").write_text(notify_env)
        bindir = tmp / "bin"
        bindir.mkdir(exist_ok=True)
        curl_log = tmp / "curl.log"
        fake_curl = bindir / "curl"
        fake_curl.write_text('#!/usr/bin/env bash\nprintf \'%s\\n\' "$@" > "$CURL_LOG"\nexit 0\n')
        fake_curl.chmod(0o755)
        env = dict(
            os.environ,
            GRAVE_CONF=str(conf),
            PATH=f"{bindir}:{os.environ['PATH']}",
            CURL_LOG=str(curl_log),
        )
        proc = subprocess.run(
            [str(ROOT / "bin/grave"), "notify", *args],
            env=env, capture_output=True, text=True, timeout=30,
        )
        return proc, curl_log

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)

    def test_event_without_config_is_silent_success(self):
        proc, curl_log = self.run_notify("--event", "session-exit", "t", "b")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout, "")
        self.assertFalse(curl_log.exists(), "must not attempt delivery")

    def test_manual_without_config_fails_with_guidance(self):
        proc, curl_log = self.run_notify("hello")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("notify.env", proc.stdout)
        self.assertFalse(curl_log.exists())

    def test_configured_event_delivers_with_title_header(self):
        proc, curl_log = self.run_notify(
            "--event", "session-exit", "agent session ended", "mybot",
            notify_env="NTFY_URL=https://ntfy.example/topic\n",
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        args = curl_log.read_text().splitlines()
        self.assertIn("Title: agent session ended", args)
        self.assertIn("https://ntfy.example/topic", args)
        self.assertIn("--data-raw", args)

    def test_muted_event_class_does_not_deliver(self):
        proc, curl_log = self.run_notify(
            "--event", "session-exit", "t", "b",
            notify_env="NTFY_URL=https://ntfy.example/topic\n",
            events="doctor",
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertFalse(curl_log.exists())

    def test_newlines_in_title_cannot_split_headers(self):
        proc, curl_log = self.run_notify(
            "evil\nAuthorization: Bearer stolen", "body",
            notify_env="NTFY_URL=https://ntfy.example/topic\n",
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        args = curl_log.read_text().splitlines()
        self.assertIn("Title: evil Authorization: Bearer stolen", args)
        self.assertNotIn("Authorization: Bearer stolen", args)

    def test_non_https_url_refuses_delivery(self):
        proc, curl_log = self.run_notify(
            "--event", "doctor", "t", "b",
            notify_env="NTFY_URL=file:///etc/passwd\n",
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertFalse(curl_log.exists())

    def test_token_is_sent_as_bearer_header(self):
        proc, curl_log = self.run_notify(
            "hello", "there",
            notify_env="NTFY_URL=https://ntfy.example/topic\nNTFY_TOKEN=tk_secret\n",
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("Authorization: Bearer tk_secret", curl_log.read_text().splitlines())


class AgentNotifyExecutionTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)

    def run_agent_notify(self, *args, payload="", tmux=False):
        tmp = pathlib.Path(self.tmpdir.name)
        log = tmp / "grave.log"
        fake_grave = tmp / "grave"
        fake_grave.write_text('#!/usr/bin/env bash\nprintf \'%s\\n\' "$@" > "$GRAVE_LOG"\n')
        fake_grave.chmod(0o755)
        bindir = tmp / "bin"
        bindir.mkdir()
        if tmux:
            fake_tmux = bindir / "tmux"
            fake_tmux.write_text("#!/usr/bin/env bash\nprintf '%s\\n' claude-yolo\n")
            fake_tmux.chmod(0o755)
        env = {
            **os.environ,
            "GRAVEDECAY_GRAVE": str(fake_grave),
            "GRAVE_LOG": str(log),
            "PATH": f"{bindir}:{os.environ['PATH']}",
        }
        if tmux:
            env["TMUX"] = "/tmp/tmux-1000/default,1,0"
        proc = subprocess.run(
            [str(ROOT / "bin/grave-agent-notify"), *args],
            input=payload, env=env, capture_output=True, text=True, timeout=30,
        )
        return proc, log

    def test_codex_turn_complete_maps_to_agent_done(self):
        proc, log = self.run_agent_notify("codex", '{"type":"agent-turn-complete"}', payload="")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        args = log.read_text().splitlines()
        self.assertIn("--event", args)
        self.assertIn("agent-done", args)
        self.assertIn("--link", args)
        self.assertIn("/", args)

    def test_claude_notification_maps_to_bell_and_tmux_link(self):
        proc, log = self.run_agent_notify(
            "claude", payload='{"hook_event_name":"Notification","message":"approval needed"}', tmux=True
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        args = log.read_text().splitlines()
        self.assertIn("bell", args)
        self.assertIn("/term/?arg=claude-yolo", args)
        self.assertIn("agent needs attention", args)


if __name__ == "__main__":
    unittest.main()
