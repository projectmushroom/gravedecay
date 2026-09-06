"""Exercise lifecycle isolation with real Git repos and a private tmux socket."""
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import tarfile
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from test_inbox_archive import load_dashboard

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(shutil.which("tmux") and shutil.which("jq"), "tmux and jq required")
class AgentWorktreeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.repo = self.root / "repos/project"
        self.repo.mkdir(parents=True)
        self.socket = "grave-test-" + self.root.name
        self.addCleanup(subprocess.run, ["tmux", "-L", self.socket, "kill-server"],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.conf = self.root / "grave.conf"
        self.conf.write_text("\n".join([
            "GRAVE_ROOT=" + shlex.quote(str(self.root)),
            "TMUX_SOCKET=" + self.socket, "TMUX_CONF=/dev/null",
            "BACKUP_DIR=" + str(self.root / "backups"), "BACKUP_KEEP=7",
            "AGENT_CONFIG_DIRS=(.nonexistent-test-agent)", "DOCKER_ROOTLESS=1"]) + "\n")
        self.env = dict(os.environ, GRAVE_CONF=str(self.conf),
                        GIT_AUTHOR_NAME="Test", GIT_AUTHOR_EMAIL="test@example.com",
                        GIT_COMMITTER_NAME="Test", GIT_COMMITTER_EMAIL="test@example.com")
        self.git("init")
        (self.repo / "file.txt").write_text("base\n")
        self.git("add", ".")
        self.git("commit", "-m", "base")

    def git(self, *args, cwd=None):
        return subprocess.check_output(["git", "-C", str(cwd or self.repo), *args],
                                       env=self.env, stderr=subprocess.STDOUT, text=True).strip()

    def grave(self, *args, success=True):
        p = subprocess.run([str(ROOT / "bin/grave"), *args], env=self.env,
                           capture_output=True, text=True, timeout=20)
        if success:
            self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        else:
            self.assertNotEqual(p.returncode, 0, p.stdout + p.stderr)
        return p

    def worktree(self, name="one"):
        return self.root / "worktrees/project" / name

    def new(self, name="one", *args):
        return self.grave("agents", "new", name, "--worktree", "--repo", "project", *args)

    def meta(self, name="one"):
        return json.loads((self.root / "agents" / name / "meta.json").read_text())

    def doctor_check(self):
        # Exercise the same function called by doctor without probing/mutating
        # host services or requiring an installed appliance.
        source = (ROOT / "bin/grave").read_text().split("\ncmd_agents()", 1)[0]
        return subprocess.run(["bash"], input=source + "\nagent_worktrees_check",
                              env=self.env, capture_output=True, text=True)

    def test_two_sessions_are_isolated_and_resume_keeps_metadata(self):
        self.new()
        self.new("two", "--branch", "feature/two")
        (self.worktree() / "file.txt").write_text("one's dirty work\n")
        self.assertEqual((self.worktree("two") / "file.txt").read_text(), "base\n")
        self.assertEqual((self.repo / "file.txt").read_text(), "base\n")
        self.assertEqual(self.git("symbolic-ref", "--short", "HEAD", cwd=self.worktree("two")), "feature/two")
        before = self.meta()
        self.grave("agents", "kill", "one")
        self.assertTrue(self.worktree().is_dir())
        self.grave("agents", "resume", "one")
        self.assertEqual(before, self.meta())
        self.assertEqual(self.doctor_check().returncode, 0)

    def test_rejects_unsafe_options_existing_branches_and_duplicate_sessions(self):
        for args in [("--repo", "../outside"), ("--repo", "project", "--branch", "--bad"),
                     ("--repo", "project", str(self.repo)), ("--branch", "x")]:
            self.grave("agents", "new", "invalid", *args, success=False)
        self.new()
        self.grave("agents", "new", "one", "--repo", "project", success=False)
        self.grave("agents", "new", "two", "--repo", "project", "--branch", "agent/one", success=False)
        self.assertFalse(self.worktree("two").exists())

    def test_symlink_root_and_tampered_metadata_are_refused(self):
        outside = self.root / "outside"
        outside.mkdir()
        (self.root / "worktrees").symlink_to(outside)
        self.grave("agents", "new", "one", "--repo", "project", success=False)
        self.assertEqual(list(outside.iterdir()), [])
        (self.root / "worktrees").unlink()
        self.new()
        self.grave("agents", "kill", "one")
        meta = self.meta()
        meta["dir"] = str(self.repo)
        (self.root / "agents/one/meta.json").write_text(json.dumps(meta))
        self.grave("agents", "prune")
        self.grave("agents", "resume", "one", success=False)
        self.assertTrue(self.repo.exists())
        self.assertNotEqual(self.doctor_check().returncode, 0)

    def test_missing_worktree_never_resumes_in_shared_repo(self):
        self.new()
        self.grave("agents", "kill", "one")
        self.worktree().rename(self.root / "moved")
        self.grave("agents", "resume", "one", success=False)
        self.assertNotEqual(self.doctor_check().returncode, 0)

    def test_corrupt_metadata_refuses_resume_and_fails_doctor(self):
        self.new()
        self.grave("agents", "kill", "one")
        (self.root / "agents/one/meta.json").write_text("{broken")
        self.grave("agents", "resume", "one", success=False)
        self.assertNotEqual(self.doctor_check().returncode, 0)

    def test_legacy_directory_sessions_and_exact_kill_still_work(self):
        self.grave("agents", "new", "one", str(self.repo))
        self.grave("agents", "new", "one2", str(self.repo))
        self.grave("agents", "kill", "one")
        live = subprocess.run(["tmux", "-L", self.socket, "has-session", "-t", "=one2"])
        self.assertEqual(live.returncode, 0)
        self.grave("agents", "resume", "one")
        self.assertEqual(self.meta()["dir"], str(self.repo))
        self.assertNotIn("worktree", self.meta())

    def test_concurrent_creations_do_not_overwrite_session_metadata(self):
        def create():
            return subprocess.run([str(ROOT / "bin/grave"), "agents", "new", "one",
                                   "--repo", "project"], env=self.env, capture_output=True,
                                  text=True, timeout=20).returncode
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: create(), range(2)))
        self.assertEqual(sorted(results), [0, 1])
        self.assertEqual(self.meta()["branch"], "agent/one")
        self.assertEqual(self.doctor_check().returncode, 0)

    def test_prune_retains_checkout_used_by_another_session(self):
        self.new()
        self.grave("agents", "kill", "one")
        self.grave("agents", "new", "observer", str(self.worktree()))
        self.assertIn("one: live; retained", self.grave("agents", "prune").stdout)
        self.assertTrue(self.worktree().exists())

    def test_prune_retains_live_dirty_ignored_and_unmerged_work(self):
        self.new()
        self.assertIn("live; retained", self.grave("agents", "prune").stdout)
        self.grave("agents", "kill", "one")
        (self.worktree() / "untracked.txt").write_text("precious")
        self.assertIn("local files or changes", self.grave("agents", "prune").stdout)
        (self.worktree() / "untracked.txt").unlink()
        self.git("config", "core.excludesFile", str(self.root / "ignore"))
        (self.root / "ignore").write_text("ignored.txt\n")
        (self.worktree() / "ignored.txt").write_text("also precious")
        self.assertIn("local files or changes", self.grave("agents", "prune").stdout)
        (self.worktree() / "ignored.txt").unlink()
        (self.worktree() / "file.txt").write_text("new commit\n")
        self.git("add", ".", cwd=self.worktree())
        self.git("commit", "-m", "agent work", cwd=self.worktree())
        self.assertIn("not merged", self.grave("agents", "prune").stdout)
        self.git("merge", "--ff-only", "agent/one")
        self.assertIn("unpushed", self.grave("agents", "prune").stdout)
        self.assertTrue(self.worktree().exists())

    def test_prune_clean_idle_checkout_keeps_branch_and_history(self):
        self.new()
        self.grave("agents", "kill", "one")
        p = self.grave("agents", "prune")
        version = tuple(int(n) for n in self.git("--version").split()[-1].split(".")[:2])
        if version < (2, 17):
            self.assertIn("Git 2.17+ required", p.stdout)
            self.assertTrue(self.worktree().is_dir())
        else:
            self.assertFalse(self.worktree().exists())
            self.assertTrue(self.meta()["pruned"])
            self.assertEqual(self.doctor_check().returncode, 0)
            self.grave("agents", "resume", "one", success=False)
        self.git("rev-parse", "--verify", "refs/heads/agent/one")
        self.assertTrue((self.root / "agents/one/meta.json").exists())

    def test_backup_contains_dirty_files_and_metadata(self):
        self.new()
        (self.worktree() / "file.txt").write_text("staged work\n")
        self.git("add", "file.txt", cwd=self.worktree())
        (self.worktree() / "file.txt").write_text("uncommitted work\n")
        (self.worktree() / "new.txt").write_text("untracked work\n")
        for name in ["config", "docker", "docs", "scripts", "logs"]:
            (self.root / name).mkdir(exist_ok=True)
        source = (ROOT / "bin/grave").read_text().rsplit("\ncase ", 1)[0]
        # Run the actual backup function; fake only privileged/host services.
        p = subprocess.run(["bash"], input=source +
                            "\nneed_root_helper() { :; }; dkctl() { return 1; }; cmd_backup\n",
                           env=self.env, capture_output=True, text=True, timeout=30)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        archive = next((self.root / "backups").glob("*/configs/agent-worktrees.tar.gz"))
        with tarfile.open(archive) as tar:
            self.assertEqual(tar.extractfile("worktrees/project/one/new.txt").read(), b"untracked work\n")
            self.assertEqual(tar.extractfile("worktrees/project/one/file.txt").read(), b"uncommitted work\n")
            self.assertIn("agents/one/meta.json", tar.getnames())
            self.assertIn(b"+staged work", tar.extractfile("recovery/one/staged.patch").read())
            saved = json.load(tar.extractfile("recovery/one/head.json"))
            self.assertEqual(saved["head"], self.git("rev-parse", "HEAD", cwd=self.worktree()))
            bundle = self.root / "recover.bundle"
            bundle.write_bytes(tar.extractfile("recovery/one/commits.bundle").read())
            patch_file = self.root / "staged.patch"
            patch_file.write_bytes(tar.extractfile("recovery/one/staged.patch").read())
            checkout_contents = tar.extractfile("worktrees/project/one/file.txt").read()
        self.git("bundle", "verify", str(bundle))
        restored = self.root / "restored"
        self.git("clone", str(bundle), str(restored))
        self.git("checkout", "--detach", saved["head"], cwd=restored)
        self.git("apply", "--index", str(patch_file), cwd=restored)
        (restored / "file.txt").write_bytes(checkout_contents)
        self.assertEqual(self.git("show", ":file.txt", cwd=restored), "staged work")
        self.assertEqual((restored / "file.txt").read_text(), "uncommitted work\n")
        self.assertEqual(archive.stat().st_mode & 0o777, 0o600)

    def test_dashboard_metadata_only_enriches_authorized_state(self):
        self.new()
        dash = load_dashboard(self.root)
        dash.ALLOWED_USERS = {"owner@example.com"}
        def fixture(_):
            return {"tmux": [{"name": "one"}], "agent_history": [{"name": "one"}]}
        with patch.object(dash, "_state", side_effect=fixture):
            owner = dash.state({"Tailscale-User-Login": "owner@example.com"})
            guest = dash.state({"Tailscale-User-Login": "guest@example.com"})
        self.assertEqual(owner["tmux"][0]["worktree"]["branch"], "agent/one")
        self.assertNotIn("worktree", guest["tmux"][0])
        self.assertNotIn("worktree", guest["agent_history"][0])


if __name__ == "__main__":
    unittest.main()
