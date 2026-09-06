"""Owner selection and provisioning tested with isolated system fixtures."""
import os
from pathlib import Path
import re
import shlex
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
LIB = ROOT / "libexec/install-owner.sh"


class OwnerSelectionTests(unittest.TestCase):
    def shell(self, body, *args):
        return subprocess.run(["bash", "-c", 'set -euo pipefail; source "$1"; shift;\n' + body,
                               "test", str(LIB), *args], capture_output=True, text=True)

    def choose(self, requested="", current="alice", existing="", immutable="0", answers=None):
        tty = "owner_has_tty() { return 1; }\n"
        if answers is not None:
            tty = "answers=(" + " ".join(shlex.quote(x) for x in answers) + ")\n" + '''
owner_has_tty() { return 0; }
owner_prompt() { :; }
owner_read() { printf -v "$1" '%s' "${answers[0]}"; answers=("${answers[@]:1}"); }
'''
        return self.shell(tty + 'owner_choose "$@"; echo "$OWNER_SELECTED"',
                          requested, current, existing, immutable)

    def test_interactive_default_custom_and_current(self):
        for answers, want in [([""], "grave"), (["2", "devbox"], "devbox"), (["3"], "alice")]:
            p = self.choose(answers=answers)
            self.assertEqual(p.returncode, 0, p.stderr)
            self.assertEqual(p.stdout.strip(), want)

    def test_root_can_select_grave_but_cannot_be_owner(self):
        self.assertEqual(self.choose(current="root", answers=[""]).stdout.strip(), "grave")
        self.assertNotEqual(self.choose("current", current="root").returncode, 0)

    def test_headless_fresh_install_requires_explicit_choice(self):
        self.assertNotEqual(self.choose().returncode, 0)
        for requested, want in [("grave", "grave"), ("custom", "custom"), ("current", "alice")]:
            p = self.choose(requested)
            self.assertEqual(p.returncode, 0, p.stderr)
            self.assertEqual(p.stdout.strip(), want)

    def test_existing_owner_is_preserved_and_migration_refused(self):
        p = self.choose(current="root", existing="nodep")
        self.assertEqual(p.stdout.strip(), "nodep")
        self.assertNotEqual(self.choose("grave", existing="nodep").returncode, 0)
        self.assertNotEqual(self.choose("current", existing="nodep").returncode, 0)

    def test_immutable_keeps_home_toolchain_identity(self):
        self.assertEqual(self.choose(current="deck", immutable="1").stdout.strip(), "deck")
        self.assertNotEqual(self.choose("grave", current="deck", immutable="1").returncode, 0)

    def test_rejects_unsafe_and_invalid_names(self):
        for name in ["root", "../grave", "grave;id", "-grave", "a" * 33, "grave user", "grave\nroot"]:
            self.assertNotEqual(self.choose(name).returncode, 0, name)

    def test_adoption_and_owner_record_drift(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "config").mkdir()
            (root / "config/owner").write_text("nodep\n")
            p = self.shell('systemctl() { echo User=nodep; }; stat() { echo nodep; }; owner_existing "$1"', tmp)
            self.assertEqual(p.returncode, 0, p.stderr)
            self.assertEqual(p.stdout.strip(), "nodep")
            p = self.shell('systemctl() { echo User=nodep; }; stat() { echo other; }; owner_existing "$1"', tmp)
            self.assertNotEqual(p.returncode, 0)
            (root / "config/owner").unlink()
            p = self.shell('systemctl() { echo User=nodep; }; stat() { echo nodep; }; owner_existing "$1"', tmp)
            self.assertEqual(p.stdout.strip(), "nodep")

    def test_download_only_tree_is_fresh_but_project_data_is_adoption(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "repos/gravedecay/.git").mkdir(parents=True)
            self.assertEqual(self.shell('owner_bootstrap_tree "$1"', tmp).returncode, 0)
            (root / "repos/project").mkdir()
            self.assertNotEqual(self.shell('owner_bootstrap_tree "$1"', tmp).returncode, 0)


class OwnerProvisioningTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.root = self.base / "appliance"
        self.source = self.base / "source"
        self.source.mkdir()
        self.env = dict(os.environ, GIT_AUTHOR_NAME="Test", GIT_AUTHOR_EMAIL="test@example.com",
                        GIT_COMMITTER_NAME="Test", GIT_COMMITTER_EMAIL="test@example.com")
        for args in [("init",), ("remote", "add", "origin", "https://github.com/projectmushroom/gravedecay")]:
            self.git(*args)
        (self.source / "raise.sh").write_text("#!/bin/bash\necho fixture\n")
        self.git("add", ".")
        self.git("commit", "-m", "fixture")
        (self.base / "etc/sudoers.d").mkdir(parents=True)
        # Run the real prepare function with user/password/ownership APIs
        # stubbed and all /etc writes relocated. Nothing touches host accounts.
        source = LIB.read_text().replace('[[ $EUID == 0 ]]', 'true')
        source = source.replace('/etc/gravedecay', str(self.base / "etc/gravedecay"))
        source = source.replace('/etc/sudoers.d', str(self.base / "etc/sudoers.d"))
        self.library = self.base / "owner.sh"
        self.library.write_text(source)
        self.stubs = '''
owner_has_tty() { return 0; }
owner_set_password() { touch "$fixture/password-set"; }
getent() {
  [[ -e "$fixture/account" ]] || return 2
  printf 'grave:x:1000:100:gravedecay appliance owner:%s/home:/bin/bash\n' "$fixture"
}
useradd() { touch "$fixture/account"; mkdir -p "$fixture/home"; }
id() { [[ "${1:-}" == -gn ]] && echo users || command id "$@"; }
chown() { :; }
stat() { if [[ "$*" == "-c %U $fixture/home" ]]; then echo grave; else command stat "$@"; fi; }
visudo() { [[ ! -e "$fixture/reject-sudoers" ]]; }
install() {
  local args=()
  while (($#)); do
    case "$1" in -o|-g) shift 2;; *) args+=("$1"); shift;; esac
  done
  command install "${args[@]}"
}
'''

    def git(self, *args):
        return subprocess.check_output(["git", "-C", str(self.source), *args],
                                       env=self.env, stderr=subprocess.STDOUT, text=True).strip()

    def prepare(self, extra=""):
        script = 'set -euo pipefail; source "$1"; fixture="$2";\n' + self.stubs + extra + '\nowner_prepare grave "$3" "$4"'
        return subprocess.run(["bash", "-c", script, "test", str(self.library), str(self.base),
                               str(self.root), str(self.source)], env=self.env,
                              capture_output=True, text=True, timeout=20)

    def test_creates_account_clones_exact_commit_and_records_owner(self):
        p = self.prepare()
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertTrue((self.base / "password-set").exists())
        self.assertEqual((self.root / "config/owner").read_text(), "grave\n")
        self.assertEqual((self.base / "etc/sudoers.d/40-gravedecay-owner").read_text(), "grave ALL=(root) ALL\n")
        head = subprocess.check_output(["git", "-C", str(self.root / "repos/gravedecay"), "rev-parse", "HEAD"], text=True).strip()
        self.assertEqual(head, self.git("rev-parse", "HEAD"))
        self.assertFalse((self.base / "home/.config/gh").exists())

    def test_existing_account_password_is_not_reset(self):
        (self.base / "account").touch()
        (self.base / "home").mkdir()
        p = self.prepare()
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertFalse((self.base / "password-set").exists())

    def test_unattended_creation_refuses_before_account_mutation(self):
        p = self.prepare('owner_has_tty() { return 1; }')
        self.assertNotEqual(p.returncode, 0)
        self.assertFalse((self.base / "account").exists())
        self.assertFalse(self.root.exists())

    def test_dirty_source_and_existing_data_are_preserved(self):
        (self.source / "uncommitted").write_text("keep me")
        self.assertNotEqual(self.prepare().returncode, 0)
        self.assertFalse((self.base / "account").exists())
        (self.source / "uncommitted").unlink()
        self.root.mkdir()
        (self.root / "work").write_text("keep this too")
        self.assertNotEqual(self.prepare().returncode, 0)
        self.assertEqual((self.root / "work").read_text(), "keep this too")

    def test_invalid_sudoers_is_never_installed(self):
        (self.base / "reject-sudoers").touch()
        self.assertNotEqual(self.prepare().returncode, 0)
        self.assertFalse((self.base / "etc/sudoers.d/40-gravedecay-owner").exists())

    def test_system_account_is_not_promoted_to_appliance_administrator(self):
        (self.base / "account").touch()
        (self.base / "home").mkdir()
        p = self.prepare('getent() { printf "grave:x:1:1:system:%s/home:/bin/bash\\n" "$fixture"; }')
        self.assertNotEqual(p.returncode, 0)
        self.assertFalse((self.base / "etc/sudoers.d/40-gravedecay-owner").exists())


class OwnerDoctorTests(unittest.TestCase):
    def test_doctor_detects_service_and_home_owner_drift(self):
        source = (ROOT / "bin/grave").read_text()
        fn = re.search(r'^appliance_owner_ok\(\) \{.*?^\}', source, re.M | re.S).group(0)
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            (base / "config").mkdir()
            (base / "config/owner").write_text("grave\n")
            for service, home in [("grave", "grave"), ("nodep", "grave"), ("grave", "root")]:
                script = '''
set -euo pipefail
GRAVE_ROOT="$1"
getent() { printf 'grave:x:1000:100:owner:%s:/bin/bash\n' "$GRAVE_ROOT"; }
systemctl() {
  # The network helper has no User= directive and intentionally runs as root.
  case "${@: -1}" in
    gravedecay-net) printf 'LoadState=loaded\nUser=\n' ;;
    *) printf 'LoadState=loaded\nUser=%s\n' "$service_owner" ;;
  esac
}
service_owner="$2"; home_owner="$3"
stat() { echo "$home_owner"; }
'''
                p = subprocess.run(["bash", "-c", script + fn + '\nappliance_owner_ok',
                                    "test", tmp, service, home], capture_output=True, text=True)
                self.assertEqual(p.returncode == 0, service == home == "grave", p.stderr)


class OwnerHandoffTests(unittest.TestCase):
    def test_raise_enters_recorded_owner_login_before_provisioning(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = base / "appliance"
            (root / "config").mkdir(parents=True)
            (root / "config/owner").write_text("grave\n")
            bindir = base / "bin"
            bindir.mkdir()
            programs = {
                "id": "echo root\n",
                "stat": "echo grave\n",
                "systemctl": "echo User=grave\n",
                "findmnt": "echo rw\n",
                "sudo": 'printf "%s\\n" "$@" >>"$HANDOFF_LOG"\n[[ "$1" != -iu ]] || exit 17\n',
            }
            for name, body in programs.items():
                p = bindir / name
                p.write_text("#!/bin/bash\n" + body)
                p.chmod(0o755)
            log = base / "handoff.log"
            env = dict(os.environ, PATH=f"{bindir}:{os.environ['PATH']}", HANDOFF_LOG=str(log))
            p = subprocess.run(["bash", str(ROOT / "raise.sh"), "--root", str(root),
                                "--user", "grave", "--profile", "generic"], env=env,
                               capture_output=True, text=True, timeout=10)
            self.assertEqual(p.returncode, 17, p.stdout + p.stderr)
            argv = log.read_text().splitlines()
            self.assertIn("-iu", argv)
            self.assertIn("SUDO_USER", argv)
            self.assertIn("--profile", argv)
            self.assertIn("generic", argv)
            self.assertNotIn("prepare", argv)  # adoption never provisions a replacement account
            self.assertEqual((root / "config/owner").read_text(), "grave\n")


if __name__ == '__main__':
    unittest.main()
