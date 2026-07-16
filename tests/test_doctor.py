import pathlib
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

    def test_workspace_doctor_runs_through_the_root_helper(self):
        # Regression #45: run unprivileged the workspace doctor hits root-owned
        # 0700 paths and fails on a healthy box. Route it through sudo like every
        # other `grave users` operation.
        self.assertIn('check "workspace registry and ownership" sudo -n "$0" __users doctor', GRAVE)
        self.assertNotIn('"$(dirname "$0")/grave-workspaces" doctor', GRAVE)


if __name__ == "__main__":
    unittest.main()
