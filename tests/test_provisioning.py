import pathlib
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
RAISE = (ROOT / "raise.sh").read_text()
PROFILES = [ROOT / "profiles" / f"{n}.sh" for n in ("generic", "t2-macbook", "steam-machine")]


class ProvisioningSafetyTests(unittest.TestCase):
    def test_sudoers_is_validated_before_it_is_installed(self):
        # Regression #53: installing an invalid sudoers drop-in and validating it
        # afterward bricks sudo host-wide with no rollback. Validate a temp file
        # first, then install only on success.
        self.assertIn('sudo visudo -c -f "$sudoers_tmp"', RAISE)
        self.assertIn('sudo install -m 440 -o root -g root "$sudoers_tmp" "$SUDOERS_FILE"', RAISE)
        self.assertNotIn('sudo tee "$SUDOERS_FILE"', RAISE)

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


if __name__ == "__main__":
    unittest.main()
