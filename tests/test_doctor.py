import pathlib
import re
import subprocess
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
GRAVE = (ROOT / "bin/grave").read_text()
RAISE = (ROOT / "raise.sh").read_text()


class DoctorContractTests(unittest.TestCase):
    def test_firewall_check_requires_default_deny_not_just_running(self):
        # Regression #55: a running firewall that defaults to allow-in still
        # violates the 127.0.0.1+tailnet boundary. The check must assert the
        # default-deny policy, not merely "Status: active".
        self.assertIn('grep -qE "Default: (deny|reject) \\(incoming\\)"', GRAVE)
        self.assertIn("--get-default-zone", GRAVE)
        self.assertNotIn('if command -v ufw >/dev/null; then sudo ufw status | grep -q "Status: active"', GRAVE)

    def test_reboot_readiness_does_not_fail_gaming_boot_mode(self):
        # Regression #56: `grave bootmode gaming` disables DEV_SERVICES on
        # purpose, so their enablement is the boot-mode toggle — not a
        # reboot-readiness invariant. Only ALWAYS_ON is hard-checked; DEV_SERVICES
        # is reported.
        self.assertNotIn('for u in "${ALWAYS_ON[@]}" "${DEV_SERVICES[@]}"; do', GRAVE)
        self.assertIn('for u in "${ALWAYS_ON[@]}"; do', GRAVE)
        self.assertIn('for u in "${DEV_SERVICES[@]}"; do', GRAVE)
        self.assertIn("boot mode: gaming", GRAVE)

    def test_gamewatch_doctor_only_hard_fails_when_unit_installed(self):
        # A partial/legacy install can still have a stale flag without the now-
        # universal watcher unit. Give an actionable message instead of bare
        # active/enabled failures; `gamewatch on` also refuses without the unit.
        self.assertIn("systemctl cat gravedecay-gamewatch.service >/dev/null 2>&1", GRAVE)
        self.assertIn("gravedecay-gamewatch.service not installed", GRAVE)

    def test_raise_installs_optional_gamewatch_on_every_host(self):
        # Gaming is optional policy, not a host-profile capability. Generic dev
        # boxes default off but must be able to opt in without re-profiling.
        self.assertIn('step "Optional game-mode watcher"', RAISE)
        self.assertIn('enable_restart gravedecay-gamewatch', RAISE)
        self.assertNotIn('if [[ "$IMMUTABLE" == 1 || "$PROFILE" == steam-machine ]]; then', RAISE)

    def test_doctor_compares_the_running_shell_to_the_installed_file(self):
        # The PWA page left gravedecay.py; a stale index.html is the same
        # class of bug as a stale Python process. healthz.shell is the
        # import-time sha256 of that file, matching sha256sum on disk.
        self.assertIn('check "dashboard serving installed shell"', GRAVE)
        self.assertIn("dashboard-static/index.html", GRAVE)
        self.assertIn("jq -r .shell", GRAVE)

    def test_doctor_compares_the_worker_stamp_to_the_installed_offline_page(self):
        # sw.js embeds a digest of offline.html in its cache name; a mismatch
        # means installed PWAs still pre-cache an outdated offline page.
        self.assertIn('check "dashboard serving installed offline page"', GRAVE)
        self.assertIn("dashboard-static/offline.html", GRAVE)
        self.assertIn("jq -r .sw", GRAVE)

    def test_doctor_checks_the_versioned_summary_contract(self):
        self.assertIn('check "dashboard summary API contract"', GRAVE)
        self.assertIn('/api/v1/summary', GRAVE)
        self.assertIn('.product == \\"gravedecay\\" and .api_version == 1', GRAVE)

    def test_t3_activity_doctor_keeps_the_source_boundary_and_bearer_off_argv(self):
        self.assertIn('t3_activity_configured()', GRAVE)
        self.assertIn('127\\.0\\.0\\.1|localhost', GRAVE)
        self.assertIn('\\.ts\\.net', GRAVE)
        self.assertIn('curl --config -', GRAVE)
        self.assertNotIn('curl -fsS --max-time 3 -H "Authorization: Bearer $token"', GRAVE)
        self.assertIn('if [[ -e "$T3_ACTIVITY_ENV" ]]; then', GRAVE)
        self.assertIn('check "T3 activity configuration valid" t3_activity_configured', GRAVE)

    def test_workspace_doctor_runs_through_the_root_helper(self):
        # Regression #45: run unprivileged the workspace doctor hits root-owned
        # 0700 paths and fails on a healthy box. Route it through sudo like every
        # other `grave users` operation.
        self.assertIn('workspace_doctor_ok; then ok "workspace registry and ownership"', GRAVE)
        self.assertIn('report=$(sudo -n "$0" __users doctor 2>&1) && return', GRAVE)
        self.assertNotIn('"$(dirname "$0")/grave-workspaces" doctor', GRAVE)

    def test_multi_user_doctor_enforces_the_root_loopback_boundary(self):
        self.assertIn('check "multi-user loopback boundary active"', GRAVE)
        self.assertIn('check "multi-user loopback boundary enabled"', GRAVE)
        self.assertIn('check "identity gateway enabled"', GRAVE)
        self.assertIn('check "multi-user loopback boundary rules"', GRAVE)
        self.assertIn('check "legacy T3 disabled"', GRAVE)
        self.assertIn('check "legacy terminal disabled"', GRAVE)
        self.assertIn('__boundary-status', GRAVE)
        self.assertIn('"${MULTI_USER:-0}" == 1 && "$u" == gravedecay-term', GRAVE)

    def test_legacy_disable_checks_execute_shell_negation(self):
        for name, unit in (("legacy_t3_disabled", "t3code"),
                           ("legacy_terminal_disabled", "gravedecay-term")):
            body = re.search(rf"^{name}\(\) \{{.*?^\}}", GRAVE, re.S | re.M).group(0)
            disabled = subprocess.run(["bash", "-c", f"systemctl() {{ return 1; }}\n{body}\n{name}"], capture_output=True)
            enabled = subprocess.run(["bash", "-c", f"systemctl() {{ return 0; }}\n{body}\n{name}"], capture_output=True)
            self.assertEqual(disabled.returncode, 0, unit)
            self.assertNotEqual(enabled.returncode, 0, unit)


if __name__ == "__main__":
    unittest.main()
