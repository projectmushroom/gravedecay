"""Real backups/restores in a disposable root; no host services or sudo calls."""
import fcntl
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import tempfile
import tarfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


class BackupRecoveryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        for name in ("repos", "config", "docker", "docs", "scripts", "logs", "backups", "home", "tools"):
            (self.root / name).mkdir()
        shutil.copyfile(ROOT / "libexec/backup-integrity.py", self.root / "scripts/backup-integrity.py")
        self.conf = self.root / "grave.conf"
        self.conf.write_text(
            f"GRAVE_ROOT={shlex.quote(str(self.root))}\n"
            'BACKUP_DIR="$GRAVE_ROOT/backups"\nBACKUP_KEEP=1\n'
            'AGENT_CONFIG_DIRS=()\nDOCKER_ROOTLESS=1\n')
        # Only the permission probe is permitted; all actual service operations
        # and Docker calls fail locally rather than reaching the installed host.
        for name, body in {
            "sudo": '[ "$*" = "-n systemctl --version" ]',
            "systemctl": "exit 1",
            "docker": "exit 99",
            "date": 'if [ "$*" = "+%Y%m%d-%H%M%S" ]; then echo "$TEST_BACKUP_TS"; else exec /usr/bin/date "$@"; fi',
        }.items():
            path = self.root / "tools" / name
            path.write_text("#!/bin/sh\n" + body + "\n")
            path.chmod(0o755)
        self.env = dict(os.environ, GRAVE_CONF=str(self.conf), HOME=str(self.root / "home"),
                        PATH=str(self.root / "tools") + os.pathsep + os.environ["PATH"],
                        TEST_BACKUP_TS="20260913-010101", GIT_CONFIG_GLOBAL="/dev/null",
                        GIT_CONFIG_NOSYSTEM="1", GIT_AUTHOR_NAME="Test", GIT_AUTHOR_EMAIL="test@example.com",
                        GIT_COMMITTER_NAME="Test", GIT_COMMITTER_EMAIL="test@example.com")
        self.backups = self.root / "backups"
        self.repo = self.root / "repos/project with spaces"
        self.repo.mkdir()
        self.git("init", str(self.repo))
        (self.repo / "example").write_text("recover me\n")
        self.git("-C", str(self.repo), "add", ".")
        self.git("-C", str(self.repo), "commit", "-m", "initial")

    def git(self, *args):
        return subprocess.check_output(["git", *args], env=self.env, stderr=subprocess.DEVNULL, text=True)

    def grave(self, *args, success=True):
        result = subprocess.run([str(ROOT / "bin/grave"), *args], env=self.env,
                                cwd=self.root, text=True, capture_output=True, timeout=20)
        self.assertEqual(result.returncode == 0, success, result.stdout + result.stderr)
        return result

    def backup(self):
        self.grave("backup")
        return self.backups / self.env["TEST_BACKUP_TS"]

    def helper(self, action, backup, success=True):
        result = subprocess.run(["python3", str(ROOT / "libexec/backup-integrity.py"), action, str(backup)],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode == 0, success, result.stdout + result.stderr)
        return result

    def test_backup_roundtrip_verifies_without_original_repo(self):
        backup = self.backup()
        manifest = json.loads((backup / "manifest.json").read_text())
        self.assertEqual(manifest["version"], 2)
        self.assertIn("repos/project with spaces.bundle", manifest["artifacts"])
        self.assertEqual(backup.stat().st_mode & 0o777, 0o700)
        self.assertEqual((backup / "manifest.json").stat().st_mode & 0o777, 0o600)
        shutil.rmtree(self.repo)
        marker = (self.backups / ".last-verified").stat().st_mtime_ns
        self.grave("backup", "verify", backup.name)
        self.grave("restore", backup.name, "repo", self.repo.name)
        self.assertEqual((self.root / "repos/project with spaces-restored/example").read_text(), "recover me\n")
        self.assertEqual((self.backups / ".last-verified").stat().st_mtime_ns, marker)

    def test_private_restore_and_migration_staging_are_not_rearchived(self):
        for name in ('workspace-restores','workspace-migrations'):
            stage=self.root/'config'/name/'pending'; stage.mkdir(parents=True)
            (stage/'credentials').write_text('private retained state')
        (self.root/'config/ordinary').write_text('keep config')
        backup=self.backup()
        with tarfile.open(backup/'configs/grave-platform.tar.gz','r:gz') as archive:
            names=archive.getnames()
        self.assertIn('config/ordinary',names)
        self.assertFalse(any('workspace-restores' in name or 'workspace-migrations' in name for name in names))
        self.assertTrue((self.root/'config/workspace-restores/pending/credentials').exists())

    def test_same_size_corruption_blocks_restore_before_clone(self):
        backup = self.backup()
        bundle = backup / "repos/project with spaces.bundle"
        data = bundle.read_bytes()
        bundle.write_bytes(b"!" + data[1:])
        self.helper("check", backup)  # doctor explicitly checks inventory, not hashes
        result = self.grave("backup", "verify", backup.name, success=False)
        self.assertIn("checksum mismatch", result.stderr)
        self.grave("restore", backup.name, "repo", self.repo.name, success=False)
        self.assertFalse((self.root / "repos/project with spaces-restored").exists())

    def test_missing_extra_symlinked_and_truncated_files_fail_inventory(self):
        backup = self.backup()
        artifact = backup / "configs/grave-platform.tar.gz"
        data = artifact.read_bytes()
        for mutation in ("missing", "extra", "symlink", "truncated"):
            with self.subTest(mutation=mutation):
                if mutation == "extra":
                    (backup / "extra").write_text("unexpected")
                else:
                    artifact.unlink()
                    if mutation == "symlink":
                        artifact.symlink_to(self.conf)
                    elif mutation == "truncated":
                        artifact.write_bytes(data[:10])
                self.helper("check", backup, success=False)
                if artifact.is_symlink() or artifact.exists():
                    artifact.unlink()
                artifact.write_bytes(data)
                (backup / "extra").unlink(missing_ok=True)

    def test_failed_run_keeps_last_good_backup_and_markers(self):
        good = self.backup()
        marker = (self.backups / ".last-verified").read_bytes()
        self.env["TEST_BACKUP_TS"] = "20260913-020202"
        shutil.rmtree(self.root / "docker")  # actual tar failure, not a mocked backup
        self.grave("backup", success=False)
        self.assertTrue(good.exists())
        self.assertFalse((self.backups / self.env["TEST_BACKUP_TS"]).exists())
        self.assertEqual((self.backups / ".last-verified").read_bytes(), marker)
        self.assertEqual((self.backups / ".last-backup").read_text().strip(), good.name)
        self.assertEqual(len(list(self.backups.glob(".incomplete-*"))), 1)

    def test_successful_retention_ignores_unrelated_and_incomplete_trees(self):
        good = self.backup()
        for name in ("migration-old", "removed-workspaces", ".incomplete-old"):
            (self.backups / name).mkdir()
        self.env["TEST_BACKUP_TS"] = "20260913-020202"
        newest = self.backup()
        self.assertFalse(good.exists())
        self.assertTrue(newest.exists())
        for name in ("migration-old", "removed-workspaces", ".incomplete-old"):
            self.assertTrue((self.backups / name).is_dir())

    def test_clock_moving_backwards_never_prunes_the_just_published_backup(self):
        previous = self.backup()
        self.env["TEST_BACKUP_TS"] = "20260912-010101"
        current = self.backup()
        self.assertTrue(current.exists())
        self.assertFalse(previous.exists())
        self.assertEqual((self.backups / ".last-backup").read_text().strip(), current.name)

    def test_doctor_inventory_function_rejects_missing_backup_and_bad_pointer(self):
        backup = self.backup()
        source = (ROOT / "bin/grave").read_text().rsplit("\ncase ", 1)[0]

        def check(success):
            result = subprocess.run(["bash"], input=source + "\nbackup_latest_check\n",
                                    env=self.env, text=True, capture_output=True, timeout=10)
            self.assertEqual(result.returncode == 0, success, result.stdout + result.stderr)

        check(True)
        (backup / "configs/grave-platform.tar.gz").unlink()
        check(False)
        shutil.rmtree(backup)
        check(False)
        (self.backups / ".last-backup").write_text("../outside\n")
        check(False)

    def test_missing_volume_is_refused_without_calling_docker(self):
        backup = self.backup()
        calls = self.root / "docker-calls"
        (self.root / "tools/docker").write_text("#!/bin/sh\necho called >> " + shlex.quote(str(calls)) + "\nexit 1\n")
        result = self.grave("restore", backup.name, "volume", "missing", success=False)
        self.assertIn("no archive for volume", result.stdout)
        self.assertFalse(calls.exists())

    def test_lock_and_timestamp_collision_do_not_touch_completed_backup(self):
        good = self.backup()
        before = (good / "manifest.json").read_bytes()
        self.grave("backup", success=False)
        self.env["TEST_BACKUP_TS"] = "20260913-020202"
        with (self.backups / ".backup.lock").open("w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            result = self.grave("backup", success=False)
            self.assertIn("another backup is running", result.stdout)
        self.assertEqual((good / "manifest.json").read_bytes(), before)
        self.assertEqual(list(self.backups.glob(".incomplete-*")), [])

    def test_invalid_options_retention_and_paths_do_not_start_backups(self):
        for args in (("backup", "--typo"), ("backup", "--include-secrets", "extra"),
                     ("backup", "verify", "../outside"), ("restore", "../outside")):
            self.grave(*args, success=False)
        with self.conf.open("a") as stream:
            stream.write("BACKUP_KEEP=0\n")
        self.grave("backup", success=False)
        self.assertEqual(list(self.backups.iterdir()), [])

    def test_legacy_restore_warns_but_explicit_verify_fails(self):
        backup = self.backup()
        (backup / "manifest.json").write_text('{"version":1}')
        self.grave("backup", "verify", backup.name, success=False)
        result = self.grave("restore", backup.name, "repo", self.repo.name)
        self.assertIn("legacy backup has no checksums", result.stderr)

    def test_malformed_manifest_and_unsafe_inventory_fail_cleanly(self):
        backup = self.backup()
        path = backup / "manifest.json"
        original = json.loads(path.read_text())
        for value in ([], {"version": True}, {"version": 99},
                      {"version": 2, "artifacts": {}},
                      {"version": 2, "artifacts": {"../outside": {"size": 1, "sha256": "a" * 64}}}):
            with self.subTest(value=value):
                path.write_text(json.dumps(value))
                result = self.helper("verify", backup, success=False)
                self.assertNotIn("Traceback", result.stderr)
        path.write_text(json.dumps(original))
        moved = backup.with_name("moved")
        backup.rename(moved)
        backup.symlink_to(moved, target_is_directory=True)
        self.grave("backup", "verify", backup.name, success=False)


if __name__ == "__main__":
    unittest.main()
