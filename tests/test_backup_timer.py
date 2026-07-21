import pathlib
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
GRAVE = (ROOT / "bin/grave").read_text()
RAISE = (ROOT / "raise.sh").read_text()
SERVICE = (ROOT / "systemd/gravedecay-backup.service.tmpl").read_text()
TIMER = (ROOT / "systemd/gravedecay-backup.timer.tmpl").read_text()


class BackupTimerContractTests(unittest.TestCase):
    # #112: backups must happen without a human remembering them, and doctor
    # must be able to tell "backups quietly stopped" from "healthy".

    def test_raise_installs_and_enables_the_timer_on_every_host(self):
        self.assertIn('step "Nightly backup timer"', RAISE)
        self.assertIn("install_unit gravedecay-backup.service", RAISE)
        self.assertIn("install_unit gravedecay-backup.timer", RAISE)
        self.assertIn("enable_restart gravedecay-backup.timer", RAISE)

    def test_timer_catches_up_after_downtime(self):
        # A box asleep overnight must run the missed backup at wake/boot, not
        # silently age past the doctor freshness window.
        self.assertIn("Persistent=true", TIMER)
        self.assertIn("WantedBy=timers.target", TIMER)

    def test_failed_backup_pages_the_human(self):
        # The unit pages via the shared failure notifier, and `grave backup`
        # actually exits nonzero when any artifact fails — without both, a bad
        # backup is indistinguishable from a good one until restore day.
        self.assertIn("OnFailure=gravedecay-notify@%n.service", SERVICE)
        self.assertIn("backup incomplete", GRAVE)
        self.assertIn('failures=$((failures+1))', GRAVE)

    def test_artifacts_are_verified_not_just_written(self):
        # Bundles must replay and tarballs must list back; a truncated write
        # (disk full mid-run) otherwise looks identical to a good backup.
        # verify must run -C the source repo: bare `git bundle verify` needs a
        # repository and fails from the timer unit's cwd (/), which failed every
        # nightly backup on a box whose shell wasn't sitting in a repo.
        self.assertIn('git -C "$repo" bundle verify', GRAVE)
        self.assertNotIn(" && git bundle verify", GRAVE)
        self.assertIn("tar -tzf", GRAVE)

    def test_marker_written_only_on_clean_verified_run(self):
        # .last-verified is the doctor freshness source, so it must be the last
        # thing a fully clean run does — never written on a partial failure.
        self.assertIn('date -u +%FT%TZ >"$BACKUP_DIR/.last-verified"', GRAVE)
        marker = GRAVE.index('>"$BACKUP_DIR/.last-verified"')
        bail = GRAVE.index("backup incomplete")
        self.assertLess(bail, marker)

    def test_doctor_enforces_timer_and_freshness(self):
        self.assertIn('check "backup timer enabled"', GRAVE)
        self.assertIn('check "backup timer active"', GRAVE)
        self.assertIn(".last-verified", GRAVE)
        self.assertIn("BACKUP_MAX_AGE_DAYS", GRAVE)
        # Gated on the unit existing, so pre-timer installs get a nudge to
        # re-raise instead of a hard failure (same pattern as gamewatch, #56).
        self.assertIn("systemctl cat gravedecay-backup.timer", GRAVE)
        self.assertIn("backup timer not installed — re-run raise.sh", GRAVE)

    def test_fresh_box_without_first_backup_is_a_nudge_not_a_failure(self):
        # Right after raise the first nightly run hasn't happened yet; doctor
        # must still be able to pass (AGENTS.md step 3) while telling the human
        # how to get to green immediately.
        self.assertIn("no verified backup yet", GRAVE)

    def test_empty_repo_is_skipped_not_paged(self):
        # `git bundle create --all` errors on a commitless repo; a fresh clone
        # must not page the human every night.
        self.assertIn("rev-parse --quiet --verify HEAD", GRAVE)
        self.assertIn("has no commits — skipped", GRAVE)


if __name__ == "__main__":
    unittest.main()
