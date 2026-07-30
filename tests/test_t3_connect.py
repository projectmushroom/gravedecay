import pathlib
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
GRAVE = (ROOT / "bin/grave").read_text()
WEBTERM = (ROOT / "bin/webterm").read_text()
DASHBOARD = (ROOT / "dashboard/gravedecay.py").read_text()
GATEWAY = (ROOT / "dashboard/gateway.py").read_text()


class T3ConnectContractTests(unittest.TestCase):
    def test_connect_commands_pin_the_appliance_base_dir(self):
        # A bare `t3 connect …` links the DEFAULT profile (~/.t3) — the desktop
        # app's identity, not the appliance's. Every t3 connect invocation in
        # grave must pin --base-dir to the service instance.
        for line in GRAVE.splitlines():
            stripped = line.strip()
            if stripped.startswith("t3 connect") and "status --json 2" not in stripped:
                self.assertIn('--base-dir "$T3_BASE_DIR"', stripped, line)

    def test_doctor_enforces_every_declared_mode(self):
        # The mode file is the operator's intent; doctor must assert the CLI's
        # persisted state for all three declarations, not just "on".
        self.assertIn("t3 connect disabled (mode: off)", GRAVE)
        self.assertIn("t3 connect link desired (mode: publish)", GRAVE)
        self.assertIn("t3 connect link desired (mode: full)", GRAVE)

    def test_publish_only_mode_asserts_no_tunnel(self):
        # publish-only's whole point is notifications WITHOUT the managed
        # tunnel: a cloudflared under the appliance base-dir in that mode means
        # the box is exposed beyond the declared transport.
        self.assertIn('check "no relay tunnel process (publish-only)"', GRAVE)

    def test_doctor_pgrep_defeats_self_match(self):
        # Regression: `bash -c "! pgrep -f '<pattern>'"` matches its own argv,
        # so the mode-off check failed on every box. The bracket trick keeps
        # the pattern from matching the probing process itself.
        self.assertIn("[c]loudflared", GRAVE)
        self.assertNotIn("pgrep -f '$T3_BASE_DIR/.*cloudflared'", GRAVE)

    def test_doctor_warns_on_stray_default_profile_link(self):
        # The desktop app or a bare `t3` run creates a second Connect identity
        # outside the contract; doctor surfaces it without hard-failing
        # transient desktop use.
        self.assertIn("stray T3 Connect link on the default t3 profile", GRAVE)
        self.assertIn("outside the appliance T3 instance", GRAVE)

    def test_interactive_links_run_in_the_terminal_and_are_admin_only(self):
        # Connect linking is an out-of-band OAuth prompt (code entry), so it
        # runs in the webterm like the other auth flows — and changing the
        # box's exposure is not for developer workspaces.
        for arg in ("auth-t3publish", "auth-t3full"):
            self.assertIn(arg, WEBTERM)
            self.assertIn(arg, DASHBOARD)
        self.assertIn('auth-t3publish) [[ "$WORKSPACE_ROLE" == admin ]]', WEBTERM)
        self.assertIn('auth-t3full)    [[ "$WORKSPACE_ROLE" == admin ]]', WEBTERM)

    def test_headless_flag_is_used_for_linking(self):
        # The box has no browser: without --headless the CLI attempts a
        # loopback OAuth callback and the flow dead-ends.
        self.assertIn("connect link --headless", GRAVE)

    def test_dashboard_teardown_action_is_audited_as_administrative(self):
        self.assertIn('"t3connect-off": [GRAVE, "t3", "connect", "off"]', DASHBOARD)
        self.assertIn('"t3connect-off"', GATEWAY)

    def test_mode_file_agrees_between_grave_and_dashboard(self):
        self.assertIn('T3_CONNECT_MODE_FILE="$GRAVE_ROOT/config/t3-connect.mode"', GRAVE)
        self.assertIn('"config", "t3-connect.mode"', DASHBOARD)

    def test_t3_tile_setting_is_clamped_on_load_and_save(self):
        # t3_tile comes from a user-writable settings file; anything but the
        # two known values must degrade to the web UI, not into a tile href.
        self.assertEqual(DASHBOARD.count('not in ("pwa", "app")'), 2)

    def test_official_app_tile_href_is_a_constant(self):
        # The app-mode tile bypasses _safe_tile_url (which rightly refuses
        # non-http schemes), so its href must be a hardcoded constant — never
        # interpolated from settings or tile data.
        self.assertIn('href="t3code://"', DASHBOARD)
        self.assertNotIn("href=\"t3code://'+", DASHBOARD)

    def test_pairing_console_offers_the_official_app_deeplink(self):
        # The mobile app's citable pairing form: t3code://pair?pairingUrl=<enc>
        self.assertIn("t3code://pair?pairingUrl='+encodeURIComponent(", DASHBOARD)


if __name__ == "__main__":
    unittest.main()
