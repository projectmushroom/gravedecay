import importlib.util
import os
import pathlib
import socket
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]


def load(path, env):
    old = dict(os.environ)
    os.environ.update(env)
    try:
        spec = importlib.util.spec_from_file_location("mac_probe", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        os.environ.clear(); os.environ.update(old)


class MacosContractTests(unittest.TestCase):
    def test_dashboard_mac_mode_has_no_server_actions_and_network_default(self):
        dash = load(ROOT / "dashboard/gravedecay.py", {"GRAVEDECAY_PLATFORM": "macos"})
        self.assertEqual(dash.ACTIONS, {})
        self.assertEqual(dash.APPS, [{"name": "📡 Network", "url": "/net/"}])
        state = dash.state({"Tailscale-User-Login": "someone@example.test"})
        self.assertEqual(state["platform"], "macos")
        self.assertEqual(state["docker"]["containers"], [])
        self.assertEqual(state["tmux"], [])

    def test_darwin_network_parsers(self):
        net = load(ROOT / "dashboard/gravenet.py", {"GRAVENET_PLATFORM": "macos"})
        counters = net.parse_netstat_ib([
            "Name Mtu Network Address Ipkts Ierrs Opkts Oerrs Coll Drop Ibytes Obytes",
            "en0 1500 <Link#4> aa:bb 10 0 20 0 0 0 1000 2000",
        ])
        self.assertEqual(counters["en0"], (1000, 2000))
        info = net.parse_ifconfig("en0: flags=8863<UP>\n\tinet 192.168.1.8 netmask 0xffffff00\n\tstatus: active\n")
        self.assertEqual(info["en0"]["state"], "up")
        self.assertEqual(info["en0"]["addrs"], ["192.168.1.8/24"])
        self.assertEqual(net.parse_wifi_devices(["Hardware Port: Ethernet", "Device: en0", "Hardware Port: Wi-Fi", "Device: en1"]), {"en1"})
        old = net.run_lines
        net.run_lines = lambda cmd: ["? (192.168.1.4) at aa:bb:cc:dd:ee:ff on en6 ifscope [ethernet]"]
        try:
            self.assertEqual(net.neighbours()["192.168.1.4"]["dev"], "en6")
        finally:
            net.run_lines = old

    def test_installer_rejects_non_darwin(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = dict(os.environ, HOME=tmp, GRAVEDECAY_MAC_ROOT=str(pathlib.Path(tmp) / "root"),
                       PATH="/usr/bin:/bin")
            # Linux CI covers the real, unmodified host path. macOS uses a
            # Linux shim so contributors still exercise the guard locally.
            if os.uname().sysname == "Darwin":
                fake_bin = pathlib.Path(tmp) / "linux-bin"; fake_bin.mkdir()
                fake_uname = fake_bin / "uname"; fake_uname.write_text("#!/bin/sh\necho Linux\n")
                fake_uname.chmod(0o755)
                env["PATH"] = f"{fake_bin}:{env['PATH']}"
            result = subprocess.run(["sh", str(ROOT / "macos/install.sh"), "--no-serve", "--dry-run"],
                                    env=env, capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("requires Darwin", result.stderr)

    def test_installer_is_user_scoped_and_convergent(self):
        install = (ROOT / "macos/install.sh").read_text()
        uninstall = (ROOT / "macos/uninstall.sh").read_text()
        self.assertIn("--dashboard-only", install)
        self.assertIn("--network-only", install)
        self.assertIn("--dry-run", install)
        self.assertIn(".gravedecay-macos", install)
        self.assertIn("plutil -lint", install)
        self.assertIn("assets/gravedecay.png", install)
        self.assertIn("config/components", install)
        plist_template = (ROOT / "macos/LaunchAgents/io.gravedecay.dashboard.plist.tmpl").read_text()
        self.assertIn("@APPS@", plist_template)
        self.assertIn("<key>GRAVEDECAY_APPS</key><string></string>", plist_template.replace("@APPS@", ""))
        self.assertIn("📡 Network=/net/", plist_template.replace("@APPS@", "📡 Network=/net/"))
        self.assertIn('APPS=""; [ "$WANT_NET" = 0 ] ||', install)
        self.assertIn('uname -s)" = Darwin', install)
        self.assertIn('choose only one component mode', install)
        self.assertIn('--root PATH', uninstall)
        self.assertIn("health(){", install)
        self.assertIn("launchctl bootstrap", install)
        self.assertIn("serve_off /net", install)
        self.assertIn("/Applications/Tailscale.app/Contents/MacOS/Tailscale", install)
        self.assertNotIn("sudo", install)
        self.assertIn("--purge", uninstall)
        self.assertIn("refusing purge", uninstall)
        self.assertIn("set-path=/grave off", uninstall)
        self.assertIn("set-path=/net off", uninstall)
        status = (ROOT / "macos/status.sh").read_text()
        self.assertIn("launchctl print", status)
        self.assertIn("serve status", status)
        self.assertIn('[ "$dash" != 1 ] ||', status)
        self.assertIn('[ "$net" != 1 ] ||', status)
        self.assertIn('Serve: disabled (localhost-only)', status)
        self.assertIn('invalid component metadata', status)
        self.assertIn('Tailscale Serve: empty status', status)
        self.assertIn('[ "$SERVE" = 0 ] || [ -n "$TAILSCALE" ]', install)
        self.assertIn("os.path.realpath", install)
        self.assertIn('ROOT_CANON=$(CDPATH= cd -- "$ROOT" && pwd -P)', uninstall)
        with tempfile.TemporaryDirectory() as tmp:
            result = subprocess.run(["sh", str(ROOT / "macos/status.sh"), "--root", tmp], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0)
        with tempfile.TemporaryDirectory() as tmp:
            env = dict(os.environ, HOME=tmp, GRAVEDECAY_MAC_ROOT=str(pathlib.Path(tmp) / "root"),
                       PATH="/usr/bin:/bin")
            # Linux CI needs a Darwin shim to exercise the macOS installer's
            # local-only dry-run path without invoking launchd or Tailscale.
            fake_bin = pathlib.Path(tmp) / "darwin-bin"; fake_bin.mkdir()
            fake_uname = fake_bin / "uname"; fake_uname.write_text("#!/bin/sh\necho Darwin\n")
            fake_uname.chmod(0o755)
            darwin_env = dict(env, PATH=f"{fake_bin}:{env['PATH']}")
            result = subprocess.run(["sh", str(ROOT / "macos/install.sh"), "--no-serve", "--dry-run"],
                                    env=darwin_env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            unsafe = subprocess.run(["sh", str(ROOT / "macos/install.sh"), "--no-serve", "--dry-run", "--root", str(pathlib.Path(tmp) / "sub/..")],
                                    env=darwin_env, capture_output=True, text=True)
            self.assertNotEqual(unsafe.returncode, 0)
            (pathlib.Path(tmp) / ".gravedecay-macos").write_text("")
            refused = subprocess.run(["sh", str(ROOT / "macos/uninstall.sh"), "--purge", "--root", str(pathlib.Path(tmp) / "sub/..")],
                                     env=env, capture_output=True, text=True)
            self.assertNotEqual(refused.returncode, 0)

    def test_marked_servers_smoke_without_launchd_or_tailscale(self):
        """Real local processes, temporary state only: no launchctl/Serve calls."""
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            (root / "config").mkdir(); (root / "web/net").mkdir(parents=True)
            (root / "web/net/index.html").write_text("net")
            with socket.socket() as s1, socket.socket() as s2:
                s1.bind(("127.0.0.1", 0)); dash_port = str(s1.getsockname()[1])
                s2.bind(("127.0.0.1", 0)); net_port = str(s2.getsockname()[1])
            env = dict(os.environ, GRAVE_ROOT=tmp, GRAVEDECAY_PLATFORM="macos",
                       GRAVEDECAY_PORT=dash_port, GRAVENET_PLATFORM="macos",
                       GRAVENET_PORT=net_port, GRAVENET_WEB=str(root / "web/net"))
            procs = [subprocess.Popen(["python3", str(ROOT / "dashboard/gravedecay.py")], env=env),
                     subprocess.Popen(["python3", str(ROOT / "dashboard/gravenet.py")], env=env)]
            try:
                for port, path in ((dash_port, "/healthz"), (net_port, "/healthz"), (dash_port, "/api/state")):
                    for _ in range(30):
                        try:
                            with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=.5) as r:
                                body = r.read().decode()
                            break
                        except (urllib.error.URLError, ConnectionError): time.sleep(.1)
                    else: self.fail(f"{path} did not answer")
                self.assertIn('"platform": "macos"', body)
                with self.assertRaises(urllib.error.HTTPError) as denied:
                    urllib.request.urlopen(f"http://127.0.0.1:{dash_port}/api/admin/releases", timeout=1)
                self.assertEqual(denied.exception.code, 404)
                time.sleep(.2)
                with urllib.request.urlopen(f"http://127.0.0.1:{net_port}/events", timeout=2) as stream:
                    event = stream.readline().decode() + stream.readline().decode()
                self.assertIn('"ifaces"', event)
            finally:
                for proc in procs: proc.terminate()
                for proc in procs: proc.wait(timeout=3)


if __name__ == "__main__":
    unittest.main()
