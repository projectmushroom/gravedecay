import pathlib
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
GRAVE = (ROOT / "bin/grave").read_text()


class CliHardeningTests(unittest.TestCase):
    def test_bootmode_does_not_break_the_pipe_into_head(self):
        # Regression #57: `workspace_services | head -1` closes the pipe early,
        # SIGPIPEs the producer, and aborts `grave bootmode` under pipefail.
        self.assertNotIn("workspace_services | head -1", GRAVE)
        self.assertIn("first_workspace=$(workspace_services); first_workspace=${first_workspace%%", GRAVE)
        # tailnet_host had the same anti-pattern (used by `grave preview`).
        self.assertNotIn('sed -n \'s/.*"DNSName": *"\\([^"]*\\)\\.".*/\\1/p\' | head -1', GRAVE)

    def test_backup_retention_only_counts_timestamped_backups(self):
        # Regression #58: migration-* and removed-workspaces/ share BACKUP_DIR and
        # sort after the digits, occupying the keep slots and pruning real backups.
        self.assertIn("grep -E '^[0-9]{8}-[0-9]{6}$'", GRAVE)
        self.assertNotIn('done < <(ls -1 "$BACKUP_DIR" | sort | head -n -"$BACKUP_KEEP")', GRAVE)

    def test_agents_new_validates_name_and_dates_the_log_correctly(self):
        # Regression #59: the name is interpolated into a path and a tmux `sh -c`
        # command; and the deferred `$(date +%%Y%%m%%d)` produced literal %Y%m%d.
        self.assertIn('[[ "$name" =~ ^[A-Za-z0-9_-]+$ ]]', GRAVE)
        self.assertIn("session-$(date +%Y%m%d).log", GRAVE)
        self.assertNotIn("date +%%Y%%m%%d", GRAVE)

    def test_restore_volume_validates_the_volume_name(self):
        # Regression #59: $vol flows into `docker run -v $vol:/data` and a
        # container `sh -c`, so it must be validated (no host binds / injection).
        self.assertIn('[[ "$vol" =~ ^[A-Za-z0-9_.-]+$ ]]', GRAVE)


if __name__ == "__main__":
    unittest.main()
