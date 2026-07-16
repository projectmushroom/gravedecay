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

    def test_headless_reraise_needs_no_out_of_scope_sudo(self):
        # Regression #89: gravedecay-upgrade.service runs raise.sh with no TTY,
        # where any sudo outside the scoped NOPASSWD set dies at a password
        # prompt. Steady state must therefore skip every privileged write:
        # unit installs compare-then-tee (tee IS in the scope), the layout
        # claim checks existence+ownership, the CLI install cmp-skips, the
        # sudoers rewrite is stamp-guarded with a headless fallback, and the
        # tailscale operator/socket ops probe current state first.
        self.assertIn("install_unit() {", RAISE)
        self.assertGreaterEqual(RAISE.count("| install_unit "), 9)
        self.assertIn("install_cli() {", RAISE)
        self.assertIn('install_cli "$REPO_DIR/bin/grave" "$GRAVE_BIN"', RAISE)
        self.assertIn("layout_ok", RAISE)
        self.assertIn('stat -c %U "$GRAVE_ROOT"', RAISE)
        self.assertIn(".sudoers.stamp", RAISE)
        # The headless fallback must key on "is there a terminal to prompt on",
        # NOT on `sudo -l` — which answers allowed-at-all (true via a
        # password-requiring wheel rule) rather than allowed-passwordless, and
        # sent the first field test straight into the fatal privileged branch.
        # No -e probe on the drop-in either: /etc/sudoers.d is 0750 on stock
        # Arch, so the owner cannot stat entries.
        self.assertIn("elif [[ ! -t 0 ]] && sudo -n systemctl --version", RAISE)
        self.assertNotIn("sudo -n -l", RAISE)

    def test_sudoers_wheel_detection_survives_a_0750_dir(self):
        # Found by the #85 smoke, phase 3: stock Arch ships /etc/sudoers.d as
        # 0750, so an unprivileged plain ls silently misses the wheel file,
        # 50-gravedecay gets installed, and the later-sorting wheel rule
        # cancels the scoped NOPASSWD (SteamOS's 0755 dir was the exception
        # that hid this). The installed name is recorded in the user-side
        # stamp; live detection also tries sudo -n ls for first raises.
        self.assertIn('SUDOERS_FILE=$(head -1 "$sudoers_stamp")', RAISE)
        self.assertIn('[[ "$SUDOERS_FILE" == /etc/sudoers.d/* ]] || SUDOERS_FILE=""', RAISE)
        self.assertIn("sudo -n ls /etc/sudoers.d/", RAISE)
        self.assertIn('printf \'%s\\n%s\\n\' "$SUDOERS_FILE" "$sudoers_hash" >"$sudoers_stamp"', RAISE)
        self.assertIn("tailscale serve status >/dev/null 2>&1 || sudo tailscale set", RAISE)
        self.assertIn("stat -c '%G %a' /run/tailscale/tailscaled.sock", RAISE)
        # the classic offenders must be gone from raise.sh entirely
        self.assertNotIn("sudo sed -i", RAISE)
        self.assertNotIn("sudo tee /etc/systemd/system/gravedecay", RAISE)
        # package steps: pacman probes before any privileged install (found by
        # the #85 smoke harness — every Arch re-raise ran sudo pacman), and
        # apt-get update is non-fatal like the install below it already was
        self.assertIn('if pacman -T "${PACMAN_PKGS[@]}"', RAISE)
        self.assertNotIn("sudo apt-get update -qq\n", RAISE)

    def test_profile_conf_set_skips_when_value_already_set(self):
        # Regression #89: profiles run on every raise; conf_set must be a
        # sudo-free no-op when the key already holds the exact value.
        for profile in PROFILES:
            text = profile.read_text()
            self.assertIn(
                'grep -qxF "$1=$2" /etc/gravedecay/grave.conf 2>/dev/null && return 0',
                text, f"{profile.name}: conf_set rewrites (and sudos) unconditionally")

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
