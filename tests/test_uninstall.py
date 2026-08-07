"""`grave uninstall` — the inverse of raise.sh.

The load-bearing invariant is coverage: raise.sh grows units over time, and a
unit that uninstall doesn't know about is one that keeps running (and keeps its
ports bound) on a box the operator believes is torn down. test_every_installed_
unit_is_removable locks that, and the sync test keeps the fallback script's copy
of the list honest.

The behavioural tests run the real cmd_uninstall against stub sudo/systemctl/
docker binaries that RECORD rather than execute, so an assertion failure can
never reach into /etc.
"""

import json
import os
import pathlib
import re
import stat
import subprocess
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
GRAVE_TEXT = (ROOT / "bin/grave").read_text()
RAISE_TEXT = (ROOT / "raise.sh").read_text()
UNINSTALL_SH = ROOT / "uninstall.sh"


def unit_list(text, varname):
    """Parse a bash array literal: NAME=(\n a b\n c\n)."""
    body = re.search(rf"^{varname}=\((.*?)^\)", text, re.S | re.M).group(1)
    return {w for w in body.split() if w and not w.startswith("#")}


class UnitCoverageTests(unittest.TestCase):
    def test_every_installed_unit_is_removable(self):
        # raise.sh installs units via `install_unit <name>`. Every one must
        # appear in UNINSTALL_UNITS, or uninstall silently leaves a live
        # service behind. The tailscaled drop-in (…service.d/…conf) is not a
        # unit of ours and is removed by path, so it is excluded here.
        installed = set(re.findall(
            r"install_unit \"?([A-Za-z0-9@.\-]+\.(?:service|timer))(?![.\w/])", RAISE_TEXT))
        self.assertTrue(installed, "no units parsed out of raise.sh — parser drifted")
        self.assertIn("gravedecay.service", installed, "parser drifted")
        removable = unit_list(GRAVE_TEXT, "UNINSTALL_UNITS")
        missing = installed - removable
        self.assertEqual(
            missing, set(),
            f"raise.sh installs {sorted(missing)} but `grave uninstall` never removes them",
        )

    def test_fallback_script_list_matches_the_cli(self):
        # uninstall.sh must be able to tear down a box whose grave.conf is gone,
        # so it carries its own copy of the list. Drift there is invisible until
        # the day it is the only path that works.
        self.assertEqual(
            unit_list(GRAVE_TEXT, "UNINSTALL_UNITS"),
            unit_list(UNINSTALL_SH.read_text(), "UNITS"),
        )

    def test_templates_are_not_stopped_directly(self):
        # `systemctl stop foo@.service` fails with "missing the instance name";
        # templates may only have their FILE removed.
        self.assertIn('[[ "$unit" == *@.service ]] && continue', GRAVE_TEXT)

    def test_cli_is_dispatched_and_documented(self):
        self.assertIn("uninstall)  shift || true; cmd_uninstall", GRAVE_TEXT)
        self.assertIn("grave uninstall", GRAVE_TEXT)

    def test_uninstall_script_is_executable(self):
        self.assertTrue(os.access(UNINSTALL_SH, os.X_OK), "uninstall.sh must be chmod +x")


class DocsContractTests(unittest.TestCase):
    DOC = (ROOT / "docs/UNINSTALL.md").read_text()

    def test_uninstall_never_executes_a_tailscale_logout(self):
        # The doc's central promise: uninstall never drops the link the operator
        # is watching it through. `logout` may only ever appear inside advice
        # printed to the human, never as a command this script runs.
        body = re.search(r"^cmd_uninstall\(\).*?^}", GRAVE_TEXT, re.S | re.M).group(0)
        offenders = [
            line.strip() for line in body.splitlines()
            if re.search(r"tailscale (logout|down)\b", line)
            # a line that prints (say/echo) or comments is advice, not an action
            and not re.search(r"(^\s*#|\bsay\b|\becho\b)", line)
        ]
        self.assertEqual(offenders, [], "uninstall must never log the node out")
        self.assertIn("sudo tailscale logout", self.DOC, "the manual step must stay documented")

    def test_doc_and_help_agree_on_the_flags(self):
        for flag in ("--purge", "--dry-run", "--yes"):
            self.assertIn(flag, self.DOC, f"{flag} is undocumented")
            self.assertIn(flag, GRAVE_TEXT)

    def test_purge_is_refused_by_the_fallback_path(self):
        # uninstall.sh cannot know GRAVE_ROOT without the conf, so it must not
        # guess at a directory to delete — the doc says so; enforce it.
        self.assertIn("not honored", UNINSTALL_SH.read_text().lower())
        self.assertIn("not** honored", self.DOC.lower())

    def test_readme_links_the_doc(self):
        self.assertIn("docs/UNINSTALL.md", (ROOT / "README.md").read_text())


class UninstallRunTests(unittest.TestCase):
    """Drive the real command with recording stubs."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        # .resolve(): on macOS /var/folders is a symlink to /private/var, and
        # `readlink -f "$0"` inside grave reports the resolved form.
        base = pathlib.Path(self.tmp.name).resolve()
        self.home = base / "home"
        self.bin = base / "bin"
        self.groot = base / "grave-root"
        self.calls = base / "calls.log"
        for d in (self.home, self.bin):
            d.mkdir(parents=True)
        for d in ("config/secrets", "logs", "repos", "docker/core", "backups"):
            (self.groot / d).mkdir(parents=True)

        # A copy, never the repo's own bin/grave — cmd_uninstall deletes the
        # binary it is running from as its last act.
        self.grave = base / "grave"
        self.grave.write_bytes((ROOT / "bin/grave").read_bytes())
        self.grave.chmod(0o755)
        (base / "grave-workspaces").write_text("#!/bin/sh\n")

        self.conf = base / "grave.conf"
        self.conf.write_text(
            f'GRAVE_ROOT="{self.groot}"\n'
            'DEV_SERVICES=(t3code)\n'
            'DEV_STACKS=(core)\n'
            'DOCKER_ROOTLESS=1\n'
            'DOCKER_HOST=""\n'
            'TOOL_PATH=""\n'
            'ALWAYS_ON=(tailscaled)\n'
            'TMUX_SOCKET="agents"\n'
            'AGENT_CONFIG_DIRS=(.claude)\n'
            f'BACKUP_DIR="{self.groot}/backups"\n'
            'BACKUP_KEEP=7\n'
            'T3_PORT=4711\nDASH_PORT=4712\nTERM_PORT=4713\nNET_PORT=4714\n'
            'MULTI_USER=0\nGATEWAY_PORT=4710\n'
            'PREVIEW_RANGE=3000-3999\nPREVIEW_RESERVED="3050"\n'
            'CHECK_SLEEP_MASKED=1\nCHECK_SNAPPER=0\n'
        )
        self.stub("sudo", f'echo "sudo $*" >> "{self.calls}"\nexit 0\n')
        self.stub("systemctl", f'''echo "systemctl $*" >> "{self.calls}"
case "$1 $2" in
  "cat gravedecay.service"|"cat t3code.service") exit 0 ;;
  "cat "*) exit 1 ;;
esac
exit 0
''')
        # A working docker: compose down / network rm succeed, and `volume ls`
        # reports nothing so purge has no named volumes to chase.
        self.stub("docker", f'echo "docker $*" >> "{self.calls}"\nexit 0\n')
        # Not logged in — the tailnet branch is exercised separately.
        self.stub("tailscale", f'echo "tailscale $*" >> "{self.calls}"\nexit 1\n')

    def tearDown(self):
        self.tmp.cleanup()

    def stub(self, name, body):
        p = self.bin / name
        p.write_text("#!/usr/bin/env bash\n" + body)
        p.chmod(p.stat().st_mode | stat.S_IXUSR)

    def run_uninstall(self, *args, expect_ok=True):
        env = dict(os.environ)
        env.update(
            PATH=f"{self.bin}:{os.environ['PATH']}",
            HOME=str(self.home),
            GRAVE_CONF=str(self.conf),
        )
        proc = subprocess.run(
            ["bash", str(self.grave), "uninstall", *args],
            capture_output=True, text=True, env=env, input="",
        )
        if expect_ok:
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        return proc

    def recorded(self):
        return self.calls.read_text() if self.calls.exists() else ""

    # -- dry run ------------------------------------------------------------
    def test_dry_run_changes_nothing(self):
        proc = self.run_uninstall("--dry-run")
        self.assertIn("Dry run only", proc.stdout)
        self.assertTrue(self.groot.exists())
        self.assertTrue(self.grave.exists(), "dry run must not remove the CLI")
        # No mutating call may have been issued at all.
        for forbidden in ("systemctl stop", "systemctl disable", "sudo rm", "rm -f"):
            self.assertNotIn(forbidden, self.recorded())

    def test_dry_run_needs_no_confirmation(self):
        # It is the safe way to preview a teardown; prompting for it is friction.
        self.assertEqual(self.run_uninstall("--dry-run").returncode, 0)

    # -- confirmation -------------------------------------------------------
    def test_refuses_without_confirmation(self):
        proc = self.run_uninstall(expect_ok=False)
        self.assertNotEqual(proc.returncode, 0)
        self.assertTrue(self.groot.exists())
        self.assertTrue(self.grave.exists())

    # -- default scope ------------------------------------------------------
    def test_default_keeps_grave_root(self):
        (self.groot / "repos/important.txt").write_text("work")
        proc = self.run_uninstall("--yes")
        self.assertTrue(self.groot.exists(), "default uninstall must not delete data")
        self.assertEqual((self.groot / "repos/important.txt").read_text(), "work")
        self.assertIn("kept", proc.stdout)

    def test_system_installed_cli_is_removed_with_sudo(self):
        # /usr/local/bin is not user-writable — the removal must go through sudo
        # (stubbed here), so assert on the issued call rather than the file.
        sysbin = pathlib.Path(self.tmp.name).resolve() / "sysbin"
        sysbin.mkdir()
        cli = sysbin / "grave"
        cli.write_bytes(self.grave.read_bytes())
        cli.chmod(0o755)
        (sysbin / "grave-workspaces").write_text("#!/bin/sh\n")
        sysbin.chmod(0o555)
        self.grave = cli

        self.run_uninstall("--yes")
        sysbin.chmod(0o755)   # restore before tearDown removes the tree
        rec = self.recorded()
        self.assertIn(f"sudo rm -f {cli}", rec)
        self.assertIn(f"sudo rm -f {sysbin / 'grave-workspaces'}", rec)

    def test_user_installed_cli_is_removed_without_sudo(self):
        # Immutable hosts (SteamOS) put grave in ~/.local/bin, which is
        # user-owned — removing it there must not shell out to root.
        local_bin = self.home / ".local/bin"
        local_bin.mkdir(parents=True)
        cli = local_bin / "grave"
        cli.write_bytes(self.grave.read_bytes())
        cli.chmod(0o755)
        (local_bin / "grave-workspaces").write_text("#!/bin/sh\n")
        self.grave = cli

        self.run_uninstall("--yes")
        self.assertFalse(cli.exists(), "user-owned CLI should be removed directly")
        self.assertFalse((local_bin / "grave-workspaces").exists())
        # sudo is still used for /etc — just never for the user-owned CLI.
        self.assertNotIn(f"sudo rm -f {cli}", self.recorded())

    def test_platform_units_are_stopped_and_files_removed(self):
        self.run_uninstall("--yes")
        rec = self.recorded()
        self.assertIn("sudo systemctl stop", rec)
        self.assertIn("sudo systemctl disable", rec)
        self.assertIn("sudo systemctl daemon-reload", rec)

    def test_sudoers_dropin_both_names_removed(self):
        # raise.sh picks 50- or zz- depending on a wheel rule it may not be able
        # to read back; uninstall must clear either.
        self.run_uninstall("--yes")
        rec = self.recorded()
        self.assertIn("/etc/sudoers.d/50-gravedecay", rec)
        self.assertIn("/etc/sudoers.d/zz-gravedecay", rec)

    def test_sleep_targets_unmasked_when_the_profile_masked_them(self):
        self.run_uninstall("--yes")
        self.assertIn("unmask sleep.target", self.recorded())

    def test_sleep_targets_left_alone_when_not_ours(self):
        self.conf.write_text(self.conf.read_text().replace("CHECK_SLEEP_MASKED=1", "CHECK_SLEEP_MASKED=0"))
        self.run_uninstall("--yes")
        self.assertNotIn("unmask", self.recorded())

    def test_tailscale_is_never_logged_out(self):
        self.run_uninstall("--yes")
        self.assertNotIn("tailscale logout", self.recorded())
        self.assertNotIn("tailscale down", self.recorded())

    # -- purge --------------------------------------------------------------
    def test_purge_deletes_grave_root(self):
        (self.groot / "repos/important.txt").write_text("work")
        proc = self.run_uninstall("--purge", "--yes")
        self.assertFalse(self.groot.exists(), proc.stdout)

    def test_purge_dry_run_still_deletes_nothing(self):
        self.run_uninstall("--purge", "--dry-run")
        self.assertTrue(self.groot.exists())

    def test_purge_removes_the_projects_symlink_it_created(self):
        (self.home / "Projects").symlink_to(self.groot / "repos")
        self.run_uninstall("--purge", "--yes")
        self.assertFalse((self.home / "Projects").is_symlink())

    def test_purge_leaves_a_foreign_projects_dir_alone(self):
        # Only the symlink raise.sh made points at $GRAVE_ROOT/repos; a real
        # ~/Projects directory belongs to the human.
        (self.home / "Projects").mkdir()
        (self.home / "Projects/mine.txt").write_text("x")
        self.run_uninstall("--purge", "--yes")
        self.assertTrue((self.home / "Projects/mine.txt").exists())

    # -- agent hooks --------------------------------------------------------
    def test_agent_hooks_are_stripped_but_foreign_hooks_survive(self):
        claude = self.home / ".claude"
        claude.mkdir()
        settings = claude / "settings.json"
        settings.write_text(json.dumps({
            "model": "opus",
            "hooks": {
                "Stop": [
                    {"matcher": "", "hooks": [{"type": "command", "command": f"{self.groot}/scripts/grave-agent-notify claude"}]},
                    {"matcher": "", "hooks": [{"type": "command", "command": "/usr/local/bin/mine"}]},
                ],
                "Notification": [
                    {"matcher": "", "hooks": [{"type": "command", "command": f"{self.groot}/scripts/grave-agent-notify claude"}]},
                ],
            },
        }))
        codex = self.home / ".codex"
        codex.mkdir()
        (codex / "config.toml").write_text(
            'model = "gpt-5"\n'
            f'notify = ["{self.groot}/scripts/grave-agent-notify", "codex"]\n'
        )

        self.run_uninstall("--yes")

        data = json.loads(settings.read_text())
        self.assertEqual(data["model"], "opus")
        flat = json.dumps(data)
        self.assertNotIn("grave-agent-notify", flat)
        self.assertIn("/usr/local/bin/mine", flat, "a hand-added hook must survive")
        self.assertNotIn("Notification", data.get("hooks", {}), "an emptied event key should be pruned")

        toml = (codex / "config.toml").read_text()
        self.assertNotIn("grave-agent-notify", toml)
        self.assertIn('model = "gpt-5"', toml)

    def test_invalid_settings_json_is_left_untouched(self):
        claude = self.home / ".claude"
        claude.mkdir()
        settings = claude / "settings.json"
        settings.write_text("{ this is not json, grave-agent-notify")
        self.run_uninstall("--yes", expect_ok=False)
        self.assertIn("grave-agent-notify", settings.read_text())


if __name__ == "__main__":
    unittest.main()
