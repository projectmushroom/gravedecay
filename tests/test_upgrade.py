import json
import os
import pathlib
import subprocess
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
GRAVE = ROOT / "bin/grave"


class UpgradeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = pathlib.Path(self.tmp.name)
        self.remote = root / "remote.git"
        source = root / "source"
        self.source = source
        self.checkout = root / "checkout"
        self.grave_root = root / "grave-root"
        self.installer_marker = root / "installer-ran"
        (self.grave_root / "logs").mkdir(parents=True)
        subprocess.run(["git", "init", "--bare", str(self.remote)], check=True,
                       stdout=subprocess.DEVNULL)
        subprocess.run(["git", "init", str(source)], check=True,
                       stdout=subprocess.DEVNULL)
        for repo in (self.remote, source):
            subprocess.run(["git", "-C", str(repo), "symbolic-ref", "HEAD", "refs/heads/master"], check=True)
        subprocess.run(["git", "-C", str(source), "config", "user.email", "test@example.com"], check=True)
        subprocess.run(["git", "-C", str(source), "config", "user.name", "Test"], check=True)
        (source / "raise.sh").write_text(
            '#!/usr/bin/env bash\nprintf "raised\\n" >> "$UPGRADE_TEST_MARKER"\n'
        )
        subprocess.run(["git", "-C", str(source), "add", "raise.sh"], check=True)
        subprocess.run(["git", "-C", str(source), "commit", "-m", "v0.4"], check=True,
                       stdout=subprocess.DEVNULL)
        subprocess.run(["git", "-C", str(source), "tag", "v0.4.0"], check=True)
        (source / "version").write_text("v0.5.0\n")
        subprocess.run(["git", "-C", str(source), "add", "version"], check=True)
        subprocess.run(["git", "-C", str(source), "commit", "-m", "v0.5"], check=True,
                       stdout=subprocess.DEVNULL)
        subprocess.run(["git", "-C", str(source), "tag", "v0.5.0"], check=True)
        subprocess.run(["git", "-C", str(source), "remote", "add", "origin", str(self.remote)], check=True)
        subprocess.run(["git", "-C", str(source), "push", "-q", "--tags", "-u", "origin", "master"], check=True)
        subprocess.run(["git", "clone", "-q", str(self.remote), str(self.checkout)], check=True)
        subprocess.run(["git", "-C", str(self.checkout), "checkout", "-q", "v0.4.0"], check=True)
        self.conf = root / "grave.conf"
        self.conf.write_text(
            f'GRAVE_ROOT="{self.grave_root}"\n'
            f'REPO_DIR="{self.checkout}"\n'
            'UPGRADE_CHANNEL=release\nDOCKER_ROOTLESS=1\nTOOL_PATH=""\n'
        )
        self.env = dict(os.environ, GRAVE_CONF=str(self.conf),
                        UPGRADE_TEST_MARKER=str(self.installer_marker))

    def tearDown(self):
        self.tmp.cleanup()

    def grave(self, *args, check=True):
        return subprocess.run([str(GRAVE), *args], env=self.env, text=True,
                              capture_output=True, check=check)

    def head(self):
        return subprocess.check_output(
            ["git", "-C", str(self.checkout), "rev-parse", "HEAD"], text=True
        ).strip()

    def assert_rejected_before_install(self, result, head):
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.installer_marker.exists())
        self.assertEqual(self.head(), head)

    def test_releases_lists_stable_tags_and_current_checkout(self):
        data = json.loads(self.grave("releases", "--json").stdout)
        self.assertEqual(data["current"], "v0.4.0")
        self.assertEqual(data["releases"], ["v0.5.0", "v0.4.0"])

    def test_releases_reports_an_untagged_branch_on_old_git(self):
        subprocess.run(["git", "-C", str(self.checkout), "checkout", "-qb", "local-compat"], check=True)
        subprocess.run(["git", "-C", str(self.checkout), "-c", "user.name=Test",
                        "-c", "user.email=test@example.com", "commit", "--allow-empty", "-qm", "local fix"], check=True)
        data = json.loads(self.grave("releases", "--json").stdout)
        self.assertEqual(data["checkout"], "local-compat")

    def test_upgrade_can_pin_an_exact_release(self):
        result = self.grave("upgrade", "--tag", "v0.5.0")
        self.assertIn("checked out v0.5.0", result.stdout)
        head = subprocess.check_output(
            ["git", "-C", str(self.checkout), "describe", "--tags", "--exact-match"], text=True
        ).strip()
        self.assertEqual(head, "v0.5.0")

    def test_upgrade_preserves_nonconflicting_untracked_files(self):
        notes = self.checkout / "field-notes.txt"
        notes.write_text("keep me\n")
        directory = self.checkout / "local notes"
        directory.mkdir()
        (directory / "setup.log").write_text("keep this too\n")

        release = self.grave("upgrade", "--tag", "v0.5.0")
        edge = self.grave("upgrade", "--edge")

        self.assertIn("checked out v0.5.0", release.stdout)
        self.assertIn("checked out master", edge.stdout)
        self.assertEqual(notes.read_text(), "keep me\n")
        self.assertEqual((directory / "setup.log").read_text(), "keep this too\n")
        self.assertEqual(self.installer_marker.read_text(), "raised\nraised\n")

    def test_upgrade_rejects_tracked_changes_and_names_them(self):
        head = self.head()
        with (self.checkout / "raise.sh").open("a") as script:
            script.write("# local edit\n")
        for staged in (False, True):
            if staged:
                subprocess.run(["git", "-C", str(self.checkout), "add", "raise.sh"], check=True)
            for channel in (("--tag", "v0.5.0"), ("--edge",)):
                with self.subTest(staged=staged, channel=channel):
                    result = self.grave("upgrade", *channel, check=False)
                    self.assert_rejected_before_install(result, head)
                    self.assertIn("tracked local changes", result.stdout)
                    self.assertIn("raise.sh", result.stdout)

    def publish_collision(self):
        collision = self.source / "release-notes.txt"
        collision.write_text("from release\n")
        subprocess.run(["git", "-C", str(self.source), "add", collision.name], check=True)
        subprocess.run(["git", "-C", str(self.source), "commit", "-qm", "v0.6"], check=True)
        subprocess.run(["git", "-C", str(self.source), "tag", "v0.6.0"], check=True)
        subprocess.run(["git", "-C", str(self.source), "push", "-q", "--tags", "origin", "master"], check=True)
        local_collision = self.checkout / collision.name
        local_collision.write_text("local notes\n")
        return local_collision

    def test_upgrade_rejects_untracked_files_that_collide_with_release(self):
        local_collision = self.publish_collision()
        head = self.head()
        result = self.grave("upgrade", "--tag", "v0.6.0", check=False)

        self.assert_rejected_before_install(result, head)
        self.assertIn("could not check out v0.6.0", result.stdout)
        self.assertIn(local_collision.name, result.stdout)
        self.assertEqual(local_collision.read_text(), "local notes\n")

    def assert_edge_collision(self, during_checkout):
        local_collision = self.publish_collision()
        if during_checkout:
            subprocess.run(["git", "-C", str(self.checkout), "fetch", "-q", "origin"], check=True)
            subprocess.run(["git", "-C", str(self.checkout), "branch", "-f", "master", "origin/master"],
                           check=True, capture_output=True)
        else:
            # The local branch lacks the remote's colliding file: checkout
            # succeeds, and the subsequent fast-forward must reject it.
            subprocess.run(["git", "-C", str(self.checkout), "checkout", "-q", "master"], check=True)
        head = self.head()
        result = self.grave("upgrade", "--edge", check=False)
        self.assert_rejected_before_install(result, head)
        self.assertIn("could not check out master" if during_checkout else
                      "could not fast-forward master", result.stdout)
        self.assertIn(local_collision.name, result.stdout)
        self.assertEqual(local_collision.read_text(), "local notes\n")

    def test_edge_checkout_rejects_untracked_collision(self):
        self.assert_edge_collision(during_checkout=True)

    def test_edge_fast_forward_rejects_untracked_collision(self):
        self.assert_edge_collision(during_checkout=False)

    def test_upgrade_stops_when_git_status_fails(self):
        (self.checkout / ".git/HEAD").unlink()
        result = self.grave("upgrade", "--tag", "v0.5.0", check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("fatal:", result.stderr)
        self.assertNotIn("Fetching updates", result.stdout)
        self.assertFalse(self.installer_marker.exists())

    def test_upgrade_rejects_invalid_or_missing_tags(self):
        invalid = self.grave("upgrade", "--tag", "master", check=False)
        self.assertNotEqual(invalid.returncode, 0)
        missing = self.grave("upgrade", "--tag", "v9.9.9", check=False)
        self.assertNotEqual(missing.returncode, 0)
        self.assertIn("does not exist", missing.stdout)


if __name__ == "__main__":
    unittest.main()
