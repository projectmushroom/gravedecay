import os
import pathlib
import stat
import subprocess
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
WATCHER = ROOT / "bin" / "gravedecay-gamewatch"
GRAVE = ROOT / "bin" / "grave"
RAISE = (ROOT / "raise.sh").read_text()
STEAM_PROFILE = (ROOT / "profiles" / "steam-machine.sh").read_text()
GRAVE_TEXT = GRAVE.read_text()


class GamewatchDetectorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name) / "root"
        self.mock_bin = pathlib.Path(self.tmp.name) / "bin"
        self.root.mkdir()
        self.mock_bin.mkdir()
        self.conf = pathlib.Path(self.tmp.name) / "grave.conf"

    def tearDown(self):
        self.tmp.cleanup()

    def executable(self, name, body):
        path = self.mock_bin / name
        path.write_text("#!/usr/bin/env bash\n" + body)
        path.chmod(path.stat().st_mode | stat.S_IXUSR)
        return path

    def configure(self, signals, extra=""):
        self.conf.write_text(
            f'GRAVE_ROOT="{self.root}"\n'
            f"GAME_SIGNALS=({' '.join(signals)})\n"
            'GAME_PROC="reaper"\n'
            f"{extra}"
        )

    def run_watcher(self, command):
        env = {
            **os.environ,
            "GRAVE_CONF": str(self.conf),
            "PATH": f"{self.mock_bin}:{os.environ['PATH']}",
        }
        return subprocess.run(
            [str(WATCHER), command], env=env, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=False,
        )

    def test_probe_uses_configured_signal_order(self):
        # gamescope listed (after steam-cgroup) wins the order only when a GameMode
        # client or a Steam scope corroborates it.
        self.configure(["steam-cgroup", "gamescope", "gamemode", "process"])
        self.executable("gamescope", "exit 0\n")
        self.executable("pgrep", "exit 0\n")
        self.executable("systemctl", "exit 1\n")
        self.executable("busctl", "printf 'u 1\\n'\n")

        result = self.run_watcher("probe")

        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(result.stdout.strip(), "gamescope")

    def test_gamescope_alone_is_game_mode_not_a_game(self):
        # #233: SteamOS Game Mode always runs gamescope. No Steam scope with CPU
        # progress and GameMode ClientCount 0 must read as idle, with gamescope
        # listed and with the default list.
        self.executable("gamescope", "exit 0\n")
        self.executable("pgrep", '[[ "$*" == *gamescope* ]]\n')
        self.executable("systemctl", 'if [[ "$*" == *list-units* ]]; then exit 0; elif [[ "$*" == *show-environment* ]]; then exit 0; fi; exit 1\n')
        self.executable("busctl", "printf 'u 0\\n'\n")
        for signals in (["steam-cgroup", "gamescope", "gamemode", "process"], None):
            if signals:
                self.configure(signals)
            else:
                self.conf.write_text(f'GRAVE_ROOT="{self.root}"\nGAME_PROC="reaper"\n')
            result = self.run_watcher("probe")
            self.assertEqual(result.returncode, 1, result.stdout)
            self.assertEqual(result.stdout.strip(), "")
        self.assertIn("GAME_SIGNALS=(steam-cgroup gamemode process)", (ROOT / "config/grave.conf.example").read_text())
        self.assertIn("|| GAME_SIGNALS=(steam-cgroup gamemode process)", WATCHER.read_text())

    def test_doctor_rejects_gamescope_before_steam_cgroup(self):
        self.executable("pgrep", "exit 1\n")
        self.executable("gamescope", "exit 0\n")
        self.executable("systemctl", 'exit 0\n')
        self.executable("busctl", "printf 'u 0\\n'\n")
        cgroup = pathlib.Path(self.tmp.name) / "cgroup"; cgroup.mkdir()
        for signals in (["gamescope", "steam-cgroup", "gamemode", "process"], ["gamescope", "process"]):
            self.configure(signals, f'GAME_CGROUP_ROOT="{cgroup}"\n')
            result = self.run_watcher("doctor")
            self.assertEqual(result.returncode, 1, result.stdout)
            self.assertIn("gamescope is listed before steam-cgroup", result.stdout)
        self.configure(["steam-cgroup", "gamescope", "gamemode", "process"], f'GAME_CGROUP_ROOT="{cgroup}"\n')
        self.assertEqual(self.run_watcher("doctor").returncode, 0)
        self.configure(["steam-cgroup", "gamemode", "process"], f'GAME_CGROUP_ROOT="{cgroup}"\n')
        self.assertEqual(self.run_watcher("doctor").returncode, 0)

    def test_probe_falls_back_to_gamemode_then_exact_process(self):
        self.configure(["gamescope", "gamemode", "process"])
        self.executable("pgrep", '[[ "$*" == *reaper* ]]\n')
        self.executable(
            "busctl",
            '[[ "$*" == *get-property* ]] && { printf "u 2\\n"; exit 0; }; exit 1\n',
        )

        gamemode = self.run_watcher("probe")
        self.assertEqual(gamemode.returncode, 0, gamemode.stdout)
        self.assertEqual(gamemode.stdout.strip(), "gamemode")

        self.executable("busctl", "exit 1\n")
        process = self.run_watcher("probe")
        self.assertEqual(process.returncode, 0, process.stdout)
        self.assertEqual(process.stdout.strip(), "process")

    def test_steam_scope_requires_cpu_progress(self):
        cgroup = pathlib.Path(self.tmp.name) / "cgroup"
        scope = cgroup / "user.slice" / "steam-game"
        scope.mkdir(parents=True)
        cpu_stat = scope / "cpu.stat"
        cpu_stat.write_text("usage_usec 100\n")
        self.configure(
            ["steam-cgroup", "process"],
            f'GAME_CGROUP_ROOT="{cgroup}"\n'
            "GAME_CGROUP_SAMPLE_SECONDS=0.01\n"
            "GAME_CGROUP_MIN_CPU_USEC=10000\n",
        )
        self.executable(
            "systemctl",
            'if [[ "$*" == *list-units* ]]; then '
            'printf "app-steam-123.scope loaded active running game\\n"; '
            'elif [[ "$*" == *show-environment* ]]; then exit 0; '
            'else printf "/user.slice/steam-game\\n"; fi\n',
        )
        self.executable("pgrep", "exit 1\n")
        self.executable("sleep", f'printf "usage_usec 12100\\n" >"{cpu_stat}"\n')

        result = self.run_watcher("probe")

        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(result.stdout.strip(), "steam-cgroup")

    def test_doctor_warns_for_process_only_and_rejects_unknown_signal(self):
        self.configure(["process"])
        self.executable("pgrep", "exit 1\n")
        process_only = self.run_watcher("doctor")
        self.assertEqual(process_only.returncode, 2, process_only.stdout)
        self.assertEqual(process_only.stdout.strip(), "process")

        self.configure(["made-up-signal"])
        unknown = self.run_watcher("doctor")
        self.assertEqual(unknown.returncode, 1, unknown.stdout)
        self.assertIn("unknown configured signal", unknown.stdout)


class GamewatchPolicyAndTimerTests(unittest.TestCase):
    def minimal_conf(self, root):
        conf = root / "grave.conf"
        conf.write_text(
            f'GRAVE_ROOT="{root}"\n'
            'TOOL_PATH=""\n'
            "DOCKER_ROOTLESS=0\n"
            'TMUX_SOCKET="agents"\n'
        )
        return conf

    def test_cli_persists_explicit_off(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            (root / "config").mkdir()
            (root / "logs").mkdir()
            conf = self.minimal_conf(root)
            env = {**os.environ, "GRAVE_CONF": str(conf)}

            result = subprocess.run(
                [str(GRAVE), "gamewatch", "off"], env=env, text=True,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=False,
            )

            self.assertEqual(result.returncode, 0, result.stdout)
            self.assertEqual((root / "config" / "gamewatch.preference").read_text(), "off\n")
            self.assertFalse((root / "config" / "gamewatch.on").exists())

    def test_invalid_auto_thaw_duration_fails_before_gaming(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            (root / "config").mkdir()
            (root / "logs").mkdir()
            conf = self.minimal_conf(root)
            env = {**os.environ, "GRAVE_CONF": str(conf)}

            result = subprocess.run(
                [str(GRAVE), "gaming", "--for", "not-a-timespan"],
                env=env, text=True, stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, check=False,
            )

            self.assertEqual(result.returncode, 1, result.stdout)
            self.assertIn("invalid --for duration", result.stdout)
            self.assertNotIn("Stopping developer services", result.stdout)

    def test_raise_defaults_only_fresh_steamos_and_preserves_choices(self):
        self.assertIn('[[ "$OS_ID" == steamos ]] && STEAMOS=1', RAISE)
        self.assertIn('elif [[ "$GRAVE_CONFIG_EXISTED" == 1 ]]; then', RAISE)
        self.assertIn('elif [[ "$STEAMOS" == 1 ]]; then', RAISE)
        self.assertIn('gamewatch_preference="$(tr -d', RAISE)
        self.assertNotIn(': > "$GRAVE_ROOT/config/gamewatch.on"', STEAM_PROFILE)

    def test_auto_thaw_is_transient_visible_and_cancelled_by_developer(self):
        self.assertIn('/usr/bin/systemd-run "${run_args[@]}"', GRAVE_TEXT)
        self.assertIn('sudo -n "$grave_bin" __auto-thaw schedule', GRAVE_TEXT)
        self.assertNotIn("/usr/bin/systemd-run *", RAISE)
        self.assertIn('--on-active="$duration"', GRAVE_TEXT)
        self.assertIn('systemctl list-timers "$AUTO_THAW_TIMER"', GRAVE_TEXT)
        developer = GRAVE_TEXT.split("cmd_developer() {", 1)[1].split("\n}", 1)[0]
        self.assertIn("cancel_auto_thaw", developer)


if __name__ == "__main__":
    unittest.main()
