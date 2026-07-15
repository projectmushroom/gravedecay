import os
import pathlib
import subprocess
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
GRAVE = (ROOT / "bin/grave").read_text()
RAISE = (ROOT / "raise.sh").read_text()
TMUX = (ROOT / "config/tmux.conf").read_text()
CONF = (ROOT / "config/grave.conf.example").read_text()
NOTIFY_UNIT = (ROOT / "systemd/gravedecay-notify@.service.tmpl").read_text()

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
        self.assertIn(': "${NOTIFY_EVENTS:=session-exit bell unit-failure doctor}"', GRAVE)
        self.assertIn('NOTIFY_EVENTS="session-exit bell unit-failure doctor"', CONF)

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
        self.assertIn("/etc/systemd/system/gravedecay-notify@.service", RAISE)

    def test_doctor_checks_gate_on_the_channel_existing(self):
        # No notify.env → no checks (the feature is opt-in); with it, doctor
        # must verify reachability without publishing (?poll=1), the secret's
        # mode, and the installed notifier unit.
        self.assertIn('if [[ -e "$NOTIFY_ENV" ]]; then', GRAVE)
        self.assertIn('check "notify channel reachable"    notify_reachable', GRAVE)
        self.assertIn('"$NTFY_URL/json?poll=1"', GRAVE)
        self.assertIn("systemctl cat 'gravedecay-notify@.service'", GRAVE)

    def test_doctor_failure_pages_without_breaking_doctor(self):
        # The page runs in a subshell (cmd_notify exits) and never changes
        # doctor's own verdict.
        self.assertIn('( cmd_notify --event doctor --priority high', GRAVE)


class NotifyExecutionTests(unittest.TestCase):
    """Drive bin/grave notify with a temp conf and a fake curl on PATH."""

    def run_notify(self, *args, notify_env=None, events="session-exit bell unit-failure doctor"):
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


if __name__ == "__main__":
    unittest.main()
