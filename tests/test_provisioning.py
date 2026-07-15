import pathlib
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
RAISE = (ROOT / "raise.sh").read_text()
GRAVE = (ROOT / "bin/grave").read_text()
INSTALL = (ROOT / "install.sh").read_text()
TOOLCHAIN = (ROOT / "steamos-toolchain.sh").read_text()
T2 = (ROOT / "profiles/t2-macbook.sh").read_text()
PROFILES = [ROOT / "profiles" / f"{n}.sh" for n in ("generic", "t2-macbook", "steam-machine")]


class ProvisioningSafetyTests(unittest.TestCase):
    def test_sudoers_is_validated_before_it_is_installed(self):
        # Regression #53: installing an invalid sudoers drop-in and validating it
        # afterward bricks sudo host-wide with no rollback. Validate a temp file
        # first, then install only on success.
        self.assertIn('sudo visudo -c -f "$sudoers_tmp"', RAISE)
        self.assertIn('sudo install -m 440 -o root -g root "$sudoers_tmp" "$SUDOERS_FILE"', RAISE)
        self.assertNotIn('sudo tee "$SUDOERS_FILE"', RAISE)

    def test_sudoers_temp_file_is_created_by_root(self):
        # Regression #90: fs.protected_regular (SteamOS/Arch hardening) makes the
        # kernel refuse root's O_CREAT open of another user's file in sticky /tmp,
        # so `sudo tee` into a user-created mktemp dies with EACCES and aborts the
        # whole ritual at the sudoers step. root must create the temp file itself.
        self.assertIn('sudoers_tmp=$(sudo mktemp)', RAISE)
        self.assertNotIn('sudoers_tmp=$(mktemp)', RAISE)

    def test_conf_set_appends_a_missing_key(self):
        # Regression #52: a plain `sed s|^K=.*|` no-ops when the key is absent, so
        # a CHECK_* invariant never reaches doctor on an upgraded box with an older
        # grave.conf. conf_set must append when the key is missing.
        for profile in PROFILES:
            text = profile.read_text()
            self.assertIn('sudo tee -a /etc/gravedecay/grave.conf', text,
                          f"{profile.name}: conf_set does not append missing keys")
            self.assertNotIn(
                'conf_set() { sudo sed -i "s|^$1=.*|$1=$2|" /etc/gravedecay/grave.conf; }',
                text, f"{profile.name}: still uses the no-op-on-missing conf_set")


    def test_raise_is_idempotent_on_rerun(self):
        # Regression #61: a dangling canonical-repo symlink made `ln -s` abort;
        # `umask 077` leaked past the token write; and the t2 profile grew
        # ALWAYS_ON on every rerun.
        self.assertIn('ln -sfn "$REPO_DIR" "$CANON_REPO"', RAISE)
        self.assertNotIn('  ln -s "$REPO_DIR" "$CANON_REPO"', RAISE)
        self.assertIn("(umask 077;", RAISE)
        self.assertIn("grep -q 'amdgpu-pstate-pin' /etc/gravedecay/grave.conf", T2)

    def test_install_relocates_off_the_immutable_root(self):
        # Regression #54: install.sh mkdir'd /srv/dev before any immutability
        # check — it fails on a read-only SteamOS root, and even when it works the
        # checkout rides the root image and is wiped by the next OS update. It must
        # detect immutability and install under $HOME (no sudo for that path).
        self.assertIn("steamos-readonly status", INSTALL)
        self.assertIn('GRAVE_ROOT="$HOME/gravedecay"', INSTALL)
        self.assertIn('if [[ "$GRAVE_ROOT" == "$HOME"/* ]]; then', INSTALL)
        self.assertNotIn('DEST="$GRAVE_ROOT/repos/gravedecay"\n\n[[ $EUID', INSTALL)

    def test_debian_ssh_and_ufw_paths_are_handled(self):
        # Regression #61: OpenSSH is `ssh` on Debian (not `sshd`), and ufw lives
        # in /usr/sbin there (not /usr/bin), so a healthy Debian box reported ssh
        # missing and prompted for a password on `sudo ufw`.
        self.assertIn("SSHD_UNIT=ssh", RAISE)
        self.assertIn("/usr/bin/ufw, /usr/sbin/ufw", RAISE)
        self.assertIn("is-active --quiet sshd || systemctl is-active --quiet ssh", GRAVE)

    def test_toolchain_bootstraps_are_pinned_and_verified(self):
        # Regression #62: the SteamOS toolchain `curl | sh`'d Homebrew and Docker
        # installers from a moving HEAD with no verification, and reinstalled t3
        # every run because the presence check omitted ~/.local/bin.
        self.assertNotIn("install/HEAD/install.sh", TOOLCHAIN)
        self.assertNotIn("get.docker.com/rootless | sh", TOOLCHAIN)
        self.assertIn("fetch_verified", TOOLCHAIN)
        self.assertIn("sha256sum", TOOLCHAIN)
        self.assertIn("checksum mismatch", TOOLCHAIN)
        self.assertIn('PATH="$HOME/.local/bin:$W:', TOOLCHAIN)


if __name__ == "__main__":
    unittest.main()
