import importlib.util
import os
import pathlib
import socket
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
import json
import threading
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

    def test_macos_nested_repo_inventory_is_canonical_bounded_and_safe(self):
        with tempfile.TemporaryDirectory() as tmp:
            app_root = pathlib.Path(tmp, "app-data")
            sites = pathlib.Path(tmp, "Sites")
            repo = sites / "owner" / "project"
            repo.mkdir(parents=True)
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            subprocess.run(["git", "-C", str(repo), "remote", "add", "origin",
                            "git@github.com:example/project.git"], check=True)
            (app_root / "config").mkdir(parents=True)
            dash = load(ROOT / "dashboard/gravedecay.py", {
                "GRAVEDECAY_PLATFORM": "macos", "GRAVE_ROOT": str(app_root),
            })
            self.assertEqual(dash.macos_repo_root("relative")[0], None)
            dash.save_settings({"repo_root": str(sites)})
            inventory = dash.collect_macos_repo_inventory()
            self.assertEqual(inventory["root"], str(sites.resolve()))
            self.assertEqual(len(inventory["repos"]), 1)
            found = inventory["repos"][0]
            self.assertEqual(found["name"], "owner/project")
            self.assertEqual(found["github_repo"], "example/project")
            self.assertEqual(dash.github_remote("not-a-github-remote"), None)
            self.assertIn("MACOS_REPO_SCAN_MAX_DIRS", (ROOT / "dashboard/gravedecay.py").read_text())

    def test_macos_work_data_is_private_but_allowed_owner_can_read_it(self):
        dash = load(ROOT / "dashboard/gravedecay.py", {
            "GRAVEDECAY_PLATFORM": "macos", "GRAVEDECAY_ALLOWED_USERS": "owner@example.test",
        })
        saved = {name: getattr(dash, name) for name in (
            "collect_macos_repo_inventory", "collect_macos_work",
            "collect_linear", "collect_services", "collect_system",
        )}
        dash.collect_macos_repo_inventory = lambda: {"root": "/private/Sites", "repos": [{"name": "secret"}], "error": None}
        dash.collect_macos_work = lambda _: {"github": {"repos": [{"repo": "secret", "prs": [{"title": "secret"}]}], "error": None, "login": "owner"},
                                              "ci": {"rows": [{"repo": "secret"}], "error": None}}
        dash.collect_linear = lambda: {"configured": True, "issues": [{"title": "secret"}], "error": None}
        dash.collect_services = lambda: []
        dash.collect_system = lambda: {}
        try:
            owner = dash.state({"Tailscale-User-Login": "owner@example.test"})
            viewer = dash.state({"Tailscale-User-Login": "other@example.test"})
        finally:
            for name, fn in saved.items():
                setattr(dash, name, fn)
        self.assertEqual(owner["repos"], [{"name": "secret"}])
        self.assertEqual(owner["github"]["repos"][0]["prs"][0]["title"], "secret")
        self.assertEqual(viewer["repos"], [])
        self.assertEqual(viewer["repo_scan"]["root"], None)
        self.assertEqual(viewer["settings"]["repo_root"], "")
        self.assertEqual(viewer["github"]["error"], "restricted")
        self.assertEqual(viewer["linear"]["issues"], [])

    def test_macos_github_work_is_per_repo_sorted_and_command_bounded(self):
        dash = load(ROOT / "dashboard/gravedecay.py", {"GRAVEDECAY_PLATFORM": "macos"})
        inventory = {"root": "/private/Sites", "error": None, "repos": [
            {"name": "zeta/project", "github_repo": "example/zeta"},
            {"name": "alpha/project", "github_repo": "example/alpha"},
        ]}
        calls, old_sh, old_which = [], dash.sh, dash.shutil.which
        dash.shutil.which = lambda name: "/usr/bin/" + name
        def fake_sh(command, timeout=10):
            calls.append(command)
            if command[:4] == ["gh", "api", "user", "--jq"]:
                return 0, "owner\n", ""
            if command[1:4] == ["api", "-X", "GET"]:
                endpoint = command[4]
                if "/pulls?" in endpoint:
                    return 0, '[{"number": 1, "title": "PR", "html_url": "https://example/pr"}]', ""
            if command[1:3] == ["issue", "list"]:
                return 0, '[{"number": 2, "title": "Issue", "url": "https://example/issue"}]', ""
            if command[1:3] == ["run", "list"]:
                return 0, '[]', ""
            return 1, "", "unexpected"
        dash.sh = fake_sh
        try:
            dash._ttl_cache.pop("macos-work:/private/Sites", None)
            work = dash.collect_macos_work(inventory)
        finally:
            dash.sh, dash.shutil.which = old_sh, old_which
        groups = work["github"]["repos"]
        self.assertEqual([group["repo"] for group in groups], ["alpha/project", "zeta/project"])
        self.assertEqual(groups[0]["prs"][0]["title"], "PR")
        self.assertEqual(groups[0]["issues"][0]["title"], "Issue")
        self.assertEqual(len(calls), 1 + 3 * len(inventory["repos"]))
        self.assertEqual(dash.MACOS_GITHUB_WORKERS, 4)
        self.assertEqual(dash.MACOS_GITHUB_ITEMS_PER_REPO, 3)
        # The remote cap is visible rather than silently dropping eligible
        # repositories; its command bound still applies to the selected set.
        capped = {"root": "/private/capped", "error": None, "repos": [
            {"name": f"repo-{i:02d}", "github_repo": f"example/repo-{i:02d}"}
            for i in range(dash.MACOS_GITHUB_REPO_LIMIT + 1)
        ]}
        calls, old_sh, old_which = [], dash.sh, dash.shutil.which
        dash.sh, dash.shutil.which = fake_sh, lambda name: "/usr/bin/" + name
        try:
            capped_work = dash.collect_macos_work(capped)
        finally:
            dash.sh, dash.shutil.which = old_sh, old_which
        self.assertIn("limited to first", capped_work["github"]["warning"])
        self.assertEqual(len(capped_work["github"]["repos"]), dash.MACOS_GITHUB_REPO_LIMIT)

    def test_macos_work_settings_validate_roots_and_never_return_linear_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            app_root = pathlib.Path(tmp, "app"); (app_root / "config").mkdir(parents=True)
            sites = pathlib.Path(tmp, "Sites"); sites.mkdir()
            dash = load(ROOT / "dashboard/gravedecay.py", {
                "GRAVEDECAY_PLATFORM": "macos", "GRAVE_ROOT": str(app_root),
                "GRAVEDECAY_ALLOWED_USERS": "owner@example.test",
            })
            server = dash.ThreadingHTTPServer(("127.0.0.1", 0), dash.Handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
            origin = f"http://127.0.0.1:{server.server_port}"
            try:
                def post(data, headers=None):
                    request = urllib.request.Request(origin + "/api/settings", data=json.dumps(data).encode(),
                        headers={"Content-Type": "application/json", **(headers or {})}, method="POST")
                    return urllib.request.urlopen(request, timeout=2)
                with self.assertRaises(urllib.error.HTTPError) as bad:
                    post({"repo_root": "relative"})
                self.assertIn("absolute path", bad.exception.read().decode())
                with post({"repo_root": str(sites), "linear_key": "lin_private_test_key"}) as response:
                    body = response.read().decode()
                self.assertNotIn("lin_private_test_key", body)
                self.assertIn(str(sites.resolve()), body)
                with self.assertRaises(urllib.error.HTTPError) as remote:
                    post({}, {"Tailscale-User-Login": "other@example.test"})
                self.assertEqual(remote.exception.code, 403)
                with self.assertRaises(urllib.error.HTTPError) as cross_site:
                    post({}, {"Tailscale-User-Login": "owner@example.test", "Sec-Fetch-Site": "cross-site"})
                self.assertEqual(cross_site.exception.code, 403)
            finally:
                server.shutdown(); server.server_close(); thread.join(timeout=2)

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

    def test_dashboard_macos_metric_parsers_and_fallbacks(self):
        dash = load(ROOT / "dashboard/gravedecay.py", {"GRAVEDECAY_PLATFORM": "macos"})
        self.assertEqual(dash.parse_macos_top_cpu(
            "CPU usage: 5.24% user, 9.75% sys, 85.0% idle\n"), 15.0)
        self.assertIsNone(dash.parse_macos_top_cpu("CPU usage: unknown"))
        self.assertEqual(dash.parse_macos_memory_pressure(
            "System-wide memory free percentage: 75%\n"), 25.0)
        self.assertIsNone(dash.parse_macos_memory_pressure("not available"))
        pages, size = dash.parse_macos_vm_stat("Mach Virtual Memory Statistics: (page size of 16384 bytes)\n"
                                                "Pages free: 10.\nPages occupied by compressor: 20.\n")
        self.assertEqual((pages["occupied by compressor"], size), (20, 16384))
        thermal = dash.parse_macos_pmset_therm("CPU_Scheduler_Limit = 100\nCPU_Available_CPUs = 8\nCPU_Speed_Limit = 100\n")
        self.assertEqual((thermal["state"], thermal["speed_limit"]), ("nominal", 100))
        self.assertEqual(dash.parse_macos_pmset_therm("CPU_Speed_Limit = 75")["state"], "throttled")
        self.assertIsNone(dash.parse_macos_pmset_therm("missing")["state"])
        battery = dash.parse_macos_pmset_batt("Now drawing from 'Battery Power'\n -InternalBattery-0 87%; discharging; 2:10 remaining\n")
        self.assertEqual(battery, {"pct": 87, "state": "discharging", "power_source": "Battery Power"})
        self.assertIsNone(dash.parse_macos_pmset_batt("Now drawing from 'AC Power'"))
        self.assertEqual(dash.parse_macos_swapusage("total = 4096.00M  used = 512.00M  free = 3584.00M"),
                         {"total_mb": 4096.0, "used_mb": 512.0, "free_mb": 3584.0})
        self.assertIsNone(dash.parse_macos_swapusage("bad")["used_mb"])

    def test_dashboard_macos_render_contract(self):
        source = (ROOT / "dashboard/gravedecay.py").read_text()
        shell = (ROOT / "dashboard/static/index.html").read_text()
        self.assertIn("tile('Memory pressure'", shell)
        self.assertIn("tile('Thermal'", shell)
        self.assertIn("tile('Swap'", shell)
        self.assertIn("if(macosCompanion){", shell)
        self.assertIn("function chargeMeter(p)", shell)
        self.assertIn("chargeMeter(battery.pct)", shell)
        self.assertNotIn("meter(battery.pct)", shell)
        # Linux's established temperature/fan cards remain in its separate branch.
        self.assertIn("tile('CPU temp'", shell)
        self.assertIn("return cached(\"macos-system\", 5", source)
        self.assertIn("body:not(.macos) .mac-only-setting", shell)
        self.assertIn("Keep the Linux PR-only presentation", shell)
        self.assertIn('body.macos [data-panel="inbox"]', shell)

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
        self.assertIn("dashboard/static/", install)
        self.assertIn("scripts/dashboard-static", install)
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
        self.assertIn('Self.UserID', install)
        self.assertIn('User["<id>"].LoginName', install)
        self.assertIn('GRAVEDECAY_ALLOWED_USERS', plist_template)
        self.assertIn('/opt/homebrew/bin', plist_template)
        self.assertIn('refusing to enable Serve', install)
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

    def test_serve_installer_derives_the_exact_local_tailscale_login_or_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake_bin = pathlib.Path(tmp, "bin"); fake_bin.mkdir()
            uname = fake_bin / "uname"
            uname.write_text("#!/bin/sh\necho Darwin\n"); uname.chmod(0o755)
            tailscale = fake_bin / "tailscale"
            tailscale.write_text("#!/bin/sh\n"
                                 "if [ \"$1\" = status ]; then\n"
                                 "  printf '%s' '{\"Self\":{\"UserID\":123},\"User\":{\"123\":{\"LoginName\":\"han@projectmushroom.com\"}}}'\n"
                                 "fi\n")
            tailscale.chmod(0o755)
            env = dict(os.environ, HOME=tmp, GRAVEDECAY_MAC_ROOT=str(pathlib.Path(tmp) / "root"),
                       PATH=f"{fake_bin}:/usr/bin:/bin")
            result = subprocess.run(["sh", str(ROOT / "macos/install.sh"), "--dry-run"],
                                    env=env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertNotIn("han@projectmushroom.com", result.stdout)
            tailscale.write_text("#!/bin/sh\nprintf '%s' '{}'\n"); tailscale.chmod(0o755)
            refused = subprocess.run(["sh", str(ROOT / "macos/install.sh"), "--dry-run"],
                                     env=env, capture_output=True, text=True)
            self.assertNotEqual(refused.returncode, 0)
            self.assertIn("refusing to enable Serve", refused.stderr)

    def test_marked_servers_smoke_without_launchd_or_tailscale(self):
        """Real local processes, temporary state only: no launchctl/Serve calls."""
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            (root / "config").mkdir(); (root / "web/net").mkdir(parents=True)
            (root / "web/net/index.html").write_text("net")
            with socket.socket() as s1, socket.socket() as s2:
                s1.bind(("127.0.0.1", 0)); dash_port = str(s1.getsockname()[1])
                s2.bind(("127.0.0.1", 0)); net_port = str(s2.getsockname()[1])
            env = dict(os.environ, HOME=tmp, GRAVE_ROOT=tmp, GRAVEDECAY_PLATFORM="macos",
                       GRAVEDECAY_PORT=dash_port, GRAVENET_PLATFORM="macos",
                       GRAVENET_PORT=net_port, GRAVENET_WEB=str(root / "web/net"))
            procs = [subprocess.Popen(["python3", str(ROOT / "dashboard/gravedecay.py")], env=env),
                     subprocess.Popen(["python3", str(ROOT / "dashboard/gravenet.py")], env=env)]
            try:
                for port, path in ((dash_port, "/healthz"), (net_port, "/healthz"), (dash_port, "/api/state")):
                    for _ in range(30):
                        try:
                            # The first dashboard sample includes one native top
                            # reading; subsequent polls reuse its short TTL cache.
                            with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=3) as r:
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
