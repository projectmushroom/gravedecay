import importlib.util
import importlib.machinery
import contextlib
import io
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
import shutil

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
        self.assertEqual(dash._summary_links(), {"dashboard": "/grave/", "network": "/net/"})
        no_net = load(ROOT / "dashboard/gravedecay.py", {"GRAVEDECAY_PLATFORM": "macos", "GRAVEDECAY_APPS": ""})
        self.assertEqual(no_net._summary_links(), {"dashboard": "/grave/"})

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
                # The settings modal always sends t3_tile/yolo_apps (they ride
                # in DEFAULT_SETTINGS) — the macOS allowlist must accept them
                # or every real ⚙️ save 400s.
                with post({"repo_root": str(sites), "linear_key": "lin_private_test_key",
                           "t3_tile": "app", "yolo_apps": []}) as response:
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

    def test_macos_reliability_keepawake_doctor_and_ntfy_contract(self):
        install = (ROOT / "macos/install.sh").read_text()
        status_text = (ROOT / "macos/status.sh").read_text()
        uninstall = (ROOT / "macos/uninstall.sh").read_text()
        grave = (ROOT / "macos/grave").read_text()
        # Keep-awake ships with Serve, can be declined, and is never sudo.
        self.assertIn("--allow-sleep", install)
        self.assertIn("io.gravedecay.keepawake", install)
        self.assertIn("keepawake=%s", install)
        self.assertNotIn("sudo", install); self.assertNotIn("sudo", status_text)
        keep = (ROOT / "macos/LaunchAgents/io.gravedecay.keepawake.plist.tmpl").read_text()
        self.assertIn("caffeinate", keep); self.assertIn("-si", keep)
        doctor = (ROOT / "macos/LaunchAgents/io.gravedecay.doctor.plist.tmpl").read_text()
        self.assertIn("StartInterval", doctor); self.assertIn("status.sh", doctor); self.assertIn("--page", doctor)
        for label in ("io.gravedecay.keepawake", "io.gravedecay.doctor"):
            self.assertIn(label, uninstall); self.assertIn(label, status_text)
        self.assertIn("cmp -s", status_text); self.assertIn("notify.env", status_text)
        # The body never travels via -d, whose @ prefix reads local files.
        self.assertIn("--data-raw", grave)
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = pathlib.Path(tmp); root = tmp_path / "root"
            (root / "config/secrets").mkdir(parents=True); (root / "scripts").mkdir(); (root / "logs").mkdir()
            (root / ".gravedecay-macos").write_text("")
            fake = tmp_path / "bin"; fake.mkdir(); curl_log = tmp_path / "curl.log"
            shims = {"launchctl": "exit 0",
                     "curl": f'printf \'%s\\n\' "$@" >> "{curl_log}"; exit 0',
                     "pmset": 'echo "pid 7(caffeinate): [0x0] PreventUserIdleSystemSleep named: keepawake"',
                     "tailscale": 'printf "%s\\n" "https://mac.ts.net/grave proxy"'}
            for name, body in shims.items():
                p = fake / name; p.write_text("#!/bin/sh\n" + body + "\n"); p.chmod(0o755)
            env = dict(os.environ, HOME=str(tmp_path), PATH=f"{fake}:/usr/bin:/bin", GRAVE_ROOT=str(root))
            src = root / "repos/gravedecay"; (src / ".git").mkdir(parents=True); (src / "dashboard").mkdir()
            (src / "dashboard/gravedecay.py").write_text("code"); (root / "scripts/gravedecay.py").write_text("code")
            shutil.copy(ROOT / "macos/grave", root / "scripts/grave"); (root / "scripts/grave").chmod(0o700)
            (root / "config/components").write_text("dashboard=1\nnetwork=0\nserve=1\nkeepawake=1\n")
            run_status = lambda *extra: subprocess.run(
                ["sh", str(ROOT / "macos/status.sh"), "--root", str(root), *extra],
                env=env, capture_output=True, text=True)
            healthy = run_status()
            self.assertEqual(healthy.returncode, 0, healthy.stdout + healthy.stderr)
            # A copy the managed checkout moved past is a failing contract.
            (root / "scripts/gravedecay.py").write_text("stale")
            drifted = run_status()
            self.assertNotEqual(drifted.returncode, 0); self.assertIn("drifted", drifted.stdout)
            (root / "scripts/gravedecay.py").write_text("code")
            # Opting out of keep-awake is honored; pre-keepawake metadata on a
            # serving Mac is enforced (fails loudly when the agent is missing).
            (root / "config/components").write_text("dashboard=1\nnetwork=0\nserve=1\nkeepawake=0\n")
            opted = run_status()
            self.assertEqual(opted.returncode, 0, opted.stdout + opted.stderr); self.assertIn("opted out", opted.stdout)
            (fake / "launchctl").write_text('#!/bin/sh\ncase "$*" in *keepawake*) exit 1;; *) exit 0;; esac\n')
            (root / "config/components").write_text("dashboard=1\nnetwork=0\nserve=1\n")
            missing = run_status()
            self.assertNotEqual(missing.returncode, 0); self.assertIn("may idle-sleep", missing.stdout)
            (fake / "launchctl").write_text("#!/bin/sh\nexit 0\n")
            (root / "config/components").write_text("dashboard=1\nnetwork=0\nserve=1\nkeepawake=1\n")
            # grave notify: unconfigured --event is a silent success, direct use says how to set up.
            quiet = subprocess.run(["python3", str(ROOT / "macos/grave"), "notify", "--event", "doctor", "t"],
                                   env=env, capture_output=True, text=True)
            self.assertEqual(quiet.returncode, 0); self.assertEqual(quiet.stdout, "")
            unconfigured = subprocess.run(["python3", str(ROOT / "macos/grave"), "notify", "t"],
                                          env=env, capture_output=True, text=True)
            self.assertNotEqual(unconfigured.returncode, 0); self.assertIn("notify.env", unconfigured.stderr)
            # Configured: header values are CR/LF-stripped, priority pinned, token attached.
            notify_env = root / "config/secrets/notify.env"
            notify_env.write_text("NTFY_URL=https://ntfy.example/topic\nNTFY_TOKEN=tk_x\n"); notify_env.chmod(0o644)
            loose = run_status()
            self.assertNotEqual(loose.returncode, 0); self.assertIn("chmod 600", loose.stdout)
            notify_env.chmod(0o600)
            secure = run_status()
            self.assertEqual(secure.returncode, 0, secure.stdout + secure.stderr); self.assertIn("ntfy channel: reachable", secure.stdout)
            sent = subprocess.run(["python3", str(ROOT / "macos/grave"), "notify", "--priority", "u;rgent", "ti\ntle", "body", "words"],
                                  env=env, capture_output=True, text=True)
            self.assertEqual(sent.returncode, 0, sent.stderr)
            curl_args = curl_log.read_text()
            self.assertIn("Title: ti tle", curl_args); self.assertIn("Priority: default", curl_args)
            self.assertIn("Authorization: Bearer tk_x", curl_args); self.assertIn("body words", curl_args)
            # A failing contract with --page routes through grave notify --event doctor.
            (root / "scripts/gravedecay.py").write_text("stale"); curl_log.write_text("")
            paged = run_status("--page")
            self.assertNotEqual(paged.returncode, 0)
            self.assertIn("macOS doctor-lite failing", curl_log.read_text())

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

    def test_macos_self_update_is_user_scoped_and_fixed_path(self):
        install = (ROOT / "macos/install.sh").read_text()
        helper = (ROOT / "macos/grave").read_text()
        updater = (ROOT / "macos/updater.py").read_text()
        dash = (ROOT / "dashboard/gravedecay.py").read_text()
        plist = (ROOT / "macos/LaunchAgents/io.gravedecay.updater.plist.tmpl").read_text()
        self.assertIn("repos/gravedecay", install)
        self.assertIn("EXPLICIT_COMPONENT", install)
        self.assertIn("GRAVEDECAY_UPDATE_CHANNEL", install)
        self.assertNotIn("sudo", install + helper + updater)
        self.assertIn("v(\\d+)", helper)  # exact semantic tags only
        self.assertIn("tags.sort", helper)  # numeric ordering, not lexicographic
        self.assertIn("managed checkout origin is not trusted", helper)
        self.assertIn("update.lock", updater)
        self.assertIn("prior payload restored", updater)
        self.assertIn("65536", updater)
        self.assertIn("under(backup)", updater)
        self.assertIn("GRAVEDECAY_GRAVE", (ROOT / "macos/LaunchAgents/io.gravedecay.dashboard.plist.tmpl").read_text())
        self.assertIn("io.gravedecay.updater", plist)
        self.assertIn("MACOS_GRAVE", dash)
        self.assertIn('"/api/admin/update-status"', dash)
        self.assertIn("invalid release request", dash)
        self.assertIn("update-macos-channel", (ROOT / "dashboard/static/index.html").read_text())
        self.assertIn("io.gravedecay.updater", (ROOT / "macos/uninstall.sh").read_text())

    def test_macos_update_helper_queues_once_and_updater_preserves_public_modes(self):
        def load_script(name, path, env):
            old = dict(os.environ); os.environ.update(env)
            try:
                loader = importlib.machinery.SourceFileLoader(name, str(path))
                spec = importlib.util.spec_from_loader(name, loader); module = importlib.util.module_from_spec(spec)
                loader.exec_module(module); return module
            finally: os.environ.clear(); os.environ.update(old)
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp, "grave"); (root / "config").mkdir(parents=True); (root / ".gravedecay-macos").write_text("")
            helper = load_script("mac_helper", ROOT / "macos/grave", {"HOME": tmp, "GRAVE_ROOT": str(root)})
            helper.releases = lambda: {"channel": "edge", "releases": ["v2.0.0"]}
            calls = []
            old_run = helper.subprocess.run
            helper.subprocess.run = lambda args, **_: calls.append(args) or type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()
            helper.upgrade([])
            helper.subprocess.run = old_run
            self.assertEqual(json.loads((root / "config/update-request.json").read_text())["channel"], "edge")
            self.assertEqual(calls[0][1], "kickstart"); self.assertNotIn("-k", calls[0])
            with contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit): helper.upgrade(["--edge", "--tag", "v2.0.0"])
            for components, expected in (("dashboard=1\nnetwork=1\nserve=1\n", []),
                                         ("dashboard=1\nnetwork=0\nserve=0\n", ["--dashboard-only", "--no-serve"]),
                                         ("dashboard=0\nnetwork=1\nserve=1\n", ["--network-only"])):
                (root / "config/components").write_text(components)
                updater = load_script("mac_updater_" + str(len(expected)), ROOT / "macos/updater.py", {"GRAVE_ROOT": str(root)})
                self.assertEqual(updater.args_for_components(), expected)
            (root / "config/components").write_text("dashboard=1\nnetwork=1\nserve=x\n")
            with self.assertRaises(RuntimeError): updater.args_for_components()

    def test_helper_orders_all_semver_tags_rejects_missing_and_keeps_edge_distinct(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=pathlib.Path(tmp,"root"); (root/"config").mkdir(parents=True); (root/".gravedecay-macos").write_text("")
            helper=importlib.util.module_from_spec(spec:=importlib.util.spec_from_loader("semver_helper",importlib.machinery.SourceFileLoader("semver_helper",str(ROOT/"macos/grave")))); old=dict(os.environ); os.environ.update({"GRAVE_ROOT":str(root),"HOME":tmp})
            try: spec.loader.exec_module(helper)
            finally: os.environ.clear();os.environ.update(old)
            helper.trusted=lambda:None
            class R:
                def __init__(self,out=""): self.stdout=out; self.returncode=0; self.stderr=""
            def fake(*a,**_):
                if a[:3]==("tag","-l","v*"): return R("v0.9.0\nv0.10.0\n")
                if a[:3]==("describe","--tags","--exact-match"): return R("v0.10.0\n")
                return R("abc123\n")
            helper.git=fake; helper.write(helper.META,{"channel":"edge","checkout":"main abc123"})
            info=helper.releases(); self.assertEqual(info["releases"],["v0.10.0","v0.9.0"]); self.assertEqual(info["current"],""); self.assertEqual(info["checkout"],"main abc123")
            helper.releases=lambda:{"channel":"release","releases":["v0.10.0"]}
            with contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit): helper.upgrade(["--tag","v9.9.9"])

    def test_updater_executes_fetch_failure_and_post_cutover_rollback(self):
        def module(root, name):
            old = dict(os.environ); os.environ["GRAVE_ROOT"] = str(root)
            try:
                loader = importlib.machinery.SourceFileLoader(name, str(ROOT / "macos/updater.py"))
                spec = importlib.util.spec_from_loader(name, loader); value = importlib.util.module_from_spec(spec); loader.exec_module(value); return value
            finally: os.environ.clear(); os.environ.update(old)
        def setup(root):
            (root / "config").mkdir(parents=True); (root / "staging").mkdir(); (root / ".gravedecay-macos").write_text("")
            (root / "repos/gravedecay/.git").mkdir(parents=True)
            (root / "config/components").write_text("dashboard=1\nnetwork=0\nserve=0\n")
            (root / "config/update-request.json").write_text(json.dumps({"channel":"release","tag":None,"requested_at":int(time.time())}))
        class R:
            def __init__(self, rc=0, out=""): self.returncode, self.stdout = rc, out
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp, "root"); setup(root); updater = module(root, "fetch_failure")
            def failed_fetch(*args, **_):
                if args[1:3] == ("remote", "get-url"): return R(out=updater.ORIGIN)
                if args[1:3] == ("status", "--porcelain"): return R()
                if args[1] == "clone": return R(1, "offline")
                return R()
            updater.run = failed_fetch; old_home=os.environ.get("HOME"); os.environ["HOME"]=tmp
            try: updater.main()
            finally: os.environ["HOME"]=old_home or ""
            self.assertEqual(json.loads((root / "config/update-status.json").read_text())["state"], "failed")
            self.assertFalse((root / "config/update.lock").exists()); self.assertFalse((root / "config/update-request.json").exists())
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp, "root"); setup(root); updater = module(root, "rollback")
            def fake_run(*args, **_):
                if args[1:3] == ("remote", "get-url"): return R(out=updater.ORIGIN)
                if args[1:3] == ("status", "--porcelain"): return R()
                if args[1] == "clone" or args[1] == "checkout": return R()
                if args[1:4] == ("tag", "-l", "v*"): return R(out="v1.0.0\n")
                return R()
            installs=[]; updater.run=fake_run
            def failing_install(repo, extra, channel):
                installs.append((repo, extra, channel))
                return R(1, "bad restart") if len(installs) == 1 else R()
            updater.install = failing_install
            old_home=os.environ.get("HOME"); os.environ["HOME"]=tmp
            try: updater.main()
            finally: os.environ["HOME"]=old_home or ""
            result=json.loads((root / "config/update-status.json").read_text())
            self.assertEqual(result["state"], "failed"); self.assertTrue(result["restored"])
            self.assertTrue((root / "repos/gravedecay/.git").is_dir()); self.assertEqual(installs[0][1], ["--dashboard-only","--no-serve"])

    def test_second_updater_does_not_remove_first_workers_lock_or_request(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=pathlib.Path(tmp,"root"); (root/"config/update.lock").mkdir(parents=True); (root/".gravedecay-macos").write_text("")
            request=root/"config/update-request.json"; request.write_text(json.dumps({"channel":"release","tag":None,"requested_at":int(time.time())}))
            old=dict(os.environ); os.environ.update({"GRAVE_ROOT":str(root),"HOME":tmp})
            try:
                loader=importlib.machinery.SourceFileLoader("contended_updater",str(ROOT/"macos/updater.py")); spec=importlib.util.spec_from_loader("contended_updater",loader); updater=importlib.util.module_from_spec(spec);loader.exec_module(updater);updater.main()
            finally: os.environ.clear();os.environ.update(old)
            self.assertTrue((root/"config/update.lock").is_dir()); self.assertTrue(request.exists())

    def test_first_bootstrap_from_clean_linked_worktree_is_idempotent_and_safe(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path=pathlib.Path(tmp); source=tmp_path/"source"; shutil.copytree(ROOT,source,ignore=shutil.ignore_patterns(".git","node_modules","__pycache__"))
            subprocess.run(["git","init","-q",str(source)],check=True); subprocess.run(["git","-C",str(source),"config","user.email","test@example.test"],check=True); subprocess.run(["git","-C",str(source),"config","user.name","Test"],check=True); subprocess.run(["git","-C",str(source),"add","."],check=True); subprocess.run(["git","-C",str(source),"commit","-qm","fixture"],check=True); subprocess.run(["git","-C",str(source),"remote","add","origin","https://github.com/projectmushroom/gravedecay.git"],check=True)
            linked=tmp_path/"linked"; subprocess.run(["git","-C",str(source),"worktree","add","--detach",str(linked),"HEAD"],check=True,stdout=subprocess.DEVNULL)
            self.assertTrue((linked/".git").is_file())
            fake=tmp_path/"bin";fake.mkdir(); log=tmp_path/"launchctl.log"
            for name, body in {"uname":"echo Darwin", "plutil":"exit 0", "curl":"exit 0", "tailscale":"exit 0", "launchctl":f'printf "%s\\n" "$*" >> "{log}"'}.items():
                p=fake/name;p.write_text("#!/bin/sh\n"+body+"\n");p.chmod(0o755)
            home=tmp_path/"home";home.mkdir(); root=home/"Grave Root"; env=dict(os.environ,HOME=str(home),PATH=f"{fake}:/usr/bin:/bin")
            install=["sh",str(linked/"macos/install.sh"),"--no-serve","--root",str(root)]
            first=subprocess.run(install,env=env,capture_output=True,text=True); self.assertEqual(first.returncode,0,first.stderr)
            self.assertTrue((root/"repos/gravedecay/.git").exists()); self.assertTrue((root/".gravedecay-macos").exists()); self.assertEqual((root/"config/components").read_text(),"dashboard=1\nnetwork=1\nserve=0\nkeepawake=1\nagents=0\n")
            meta=json.loads((root/"config/release.json").read_text()); self.assertEqual(meta["current"],""); self.assertEqual(meta["kind"],"development")
            self.assertTrue((root/"scripts/grave").is_file()); self.assertTrue((root/"scripts/updater.py").is_file()); self.assertTrue((home/"Library/LaunchAgents/io.gravedecay.updater.plist").is_file()); self.assertEqual((home/".local/bin/grave").resolve(),(root/"scripts/grave").resolve())
            second=subprocess.run(install,env=env,capture_output=True,text=True); self.assertEqual(second.returncode,0,second.stderr)
            (root/"config/components").write_text("dashboard=1\nnetwork=0\nserve=0\n")
            preserved=subprocess.run(install,env=env,capture_output=True,text=True); self.assertEqual(preserved.returncode,0,preserved.stderr); self.assertEqual((root/"config/components").read_text(),"dashboard=1\nnetwork=0\nserve=0\nkeepawake=1\nagents=0\n")
            before=log.read_text(); updater=subprocess.run(["sh",str(root/"repos/gravedecay/macos/install.sh"),"--no-serve","--root",str(root)],env=dict(env,GRAVEDECAY_UPDATER="1"),capture_output=True,text=True); self.assertEqual(updater.returncode,0,updater.stderr)
            self.assertNotIn("io.gravedecay.updater",log.read_text()[len(before):])
            for kind, mutate, expected in (("dirty",lambda p:(p/"README.md").write_text("dirty"),"current source has local changes"),("untrusted",lambda p:subprocess.run(["git","-C",str(p),"remote","set-url","origin","https://example.test/nope.git"],check=True),"current source origin is not trusted")):
                badroot=home/("bad-"+kind); mutate(linked); result=subprocess.run(["sh",str(linked/"macos/install.sh"),"--no-serve","--root",str(badroot)],env=env,capture_output=True,text=True); self.assertNotEqual(result.returncode,0); self.assertIn(expected,result.stderr); subprocess.run(["git","-C",str(linked),"reset","--hard","HEAD"],check=True,stdout=subprocess.DEVNULL); subprocess.run(["git","-C",str(linked),"remote","set-url","origin","https://github.com/projectmushroom/gravedecay.git"],check=True)

    def test_macos_release_routes_are_owner_csrf_gated_and_fixed_argv(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=pathlib.Path(tmp,"app"); (root/"config").mkdir(parents=True); calls=[]
            dash=load(ROOT/"dashboard/gravedecay.py", {"GRAVEDECAY_PLATFORM":"macos","GRAVE_ROOT":str(root),"GRAVEDECAY_ALLOWED_USERS":"owner@example.test"})
            def fake(command, timeout=10):
                calls.append(command)
                if command[-1]=="--json": return 0, json.dumps({"current":"","checkout":"edge abc","channel":"edge","releases":["v0.10.0","v0.9.0"]}),""
                if command[-1]=="update-status": return 0, '{"state":"running"}',""
                return 0,"queued",""
            dash.sh=fake; server=dash.ThreadingHTTPServer(("127.0.0.1",0),dash.Handler); thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start(); origin=f"http://127.0.0.1:{server.server_port}"
            try:
                def get(path, headers={}): return urllib.request.urlopen(urllib.request.Request(origin+path,headers=headers),timeout=2)
                def post(path, data, headers={}): return urllib.request.urlopen(urllib.request.Request(origin+path,data=json.dumps(data).encode(),headers={"Content-Type":"application/json",**headers},method="POST"),timeout=2)
                self.assertEqual(get("/api/admin/releases").status,200); self.assertEqual(get("/api/admin/releases",{"Tailscale-User-Login":"owner@example.test"}).status,200); self.assertEqual(get("/api/admin/update-status").status,200)
                with self.assertRaises(urllib.error.HTTPError) as denied: get("/api/admin/releases",{"Tailscale-User-Login":"other@example.test"})
                self.assertEqual(denied.exception.code,403)
                with self.assertRaises(urllib.error.HTTPError) as denied_status: get("/api/admin/update-status",{"Tailscale-User-Login":"other@example.test"})
                self.assertEqual(denied_status.exception.code,403)
                with self.assertRaises(urllib.error.HTTPError) as denied_post: post("/api/admin/upgrade",{"channel":"edge"},{"Sec-Fetch-Site":"same-origin","Tailscale-User-Login":"other@example.test"})
                self.assertEqual(denied_post.exception.code,403)
                self.assertEqual(post("/api/admin/upgrade",{"channel":"edge"},{"Sec-Fetch-Site":"same-origin"}).status,200)
                self.assertEqual(calls[-1],[dash.MACOS_GRAVE,"upgrade","--edge"])
                self.assertEqual(post("/api/admin/upgrade",{"channel":"edge"},{"Sec-Fetch-Site":"same-origin","Tailscale-User-Login":"owner@example.test"}).status,200)
                self.assertEqual(post("/api/admin/upgrade",{"tag":"v0.10.0"},{"Sec-Fetch-Site":"same-origin"}).status,200)
                with self.assertRaises(urllib.error.HTTPError) as bad: post("/api/admin/upgrade",{"tag":"v0.10.0;rm"},{"Sec-Fetch-Site":"same-origin"})
                self.assertEqual(bad.exception.code,400)
                with self.assertRaises(urllib.error.HTTPError) as array: post("/api/admin/upgrade",[],{"Sec-Fetch-Site":"same-origin"})
                self.assertEqual(array.exception.code,400)
                with self.assertRaises(urllib.error.HTTPError) as cross: post("/api/admin/upgrade",{"channel":"edge"},{"Sec-Fetch-Site":"cross-site"})
                self.assertEqual(cross.exception.code,403)
                with self.assertRaises(urllib.error.HTTPError) as unavailable: post("/api/action",{}, {"Sec-Fetch-Site":"same-origin"})
                self.assertEqual(unavailable.exception.code,404)
            finally: server.shutdown();server.server_close();thread.join(timeout=2)

    def test_macos_agents_layer_reopens_exactly_the_gated_subset(self):
        # Observability-only (no flag): everything stays amputated.
        plain = load(ROOT / "dashboard/gravedecay.py", {"GRAVEDECAY_PLATFORM": "macos"})
        self.assertEqual(plain.ACTIONS, {})
        self.assertNotIn("t3", plain._summary_links())
        dash = load(ROOT / "dashboard/gravedecay.py", {
            "GRAVEDECAY_PLATFORM": "macos", "GRAVEDECAY_MACOS_AGENTS": "1",
            "GRAVEDECAY_ALLOWED_USERS": "owner@example.test",
        })
        # Exactly pairing comes back — never a sudo/systemd action.
        self.assertEqual(set(dash.ACTIONS), {"t3-pair"})
        self.assertEqual(dash._summary_links()["t3"], "/")
        self.assertEqual(dash._summary_links()["terminal"], "/term/")
        # Sessions are owner-private in state; a foreign viewer sees none.
        # (Stub the work collectors so state() stays hermetic and fast.)
        dash.collect_macos_repo_inventory = lambda: {"root": "/x", "repos": [], "error": None}
        dash.collect_macos_work = lambda _: {"github": {"login": None, "repos": [], "error": None},
                                             "ci": {"rows": [], "error": None}}
        dash.collect_linear = lambda: {"configured": False, "issues": [], "error": None}
        dash.collect_services = lambda: []
        dash.collect_system = lambda: {}
        dash.collect_tmux = lambda: [{"name": "claude", "windows": 1, "attached": 0}]
        owner_state = dash.state({"Tailscale-User-Login": "owner@example.test"})
        self.assertEqual(owner_state["tmux"][0]["name"], "claude")
        self.assertTrue(owner_state["macos_agents"])
        self.assertEqual(dash.state({"Tailscale-User-Login": "other@example.test"})["tmux"], [])
        kills = []
        dash.sh = lambda cmd, timeout=10: (kills.append(cmd) or (0, "", ""))
        server = dash.ThreadingHTTPServer(("127.0.0.1", 0), dash.Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        origin = f"http://127.0.0.1:{server.server_port}"
        try:
            def post(path, data, headers={}):
                return urllib.request.urlopen(urllib.request.Request(
                    origin + path, data=json.dumps(data).encode(),
                    headers={"Content-Type": "application/json", "Sec-Fetch-Site": "same-origin", **headers},
                    method="POST"), timeout=2)
            self.assertEqual(post("/api/session-kill", {"name": "claude"}).status, 200)
            self.assertEqual(kills[-1][:4], ["tmux", "-L", "agents", "kill-session"])
            self.assertEqual(post("/api/session-capture", {"name": "claude"}).status, 200)
            with self.assertRaises(urllib.error.HTTPError) as denied:
                post("/api/session-kill", {"name": "claude"}, {"Tailscale-User-Login": "other@example.test"})
            self.assertEqual(denied.exception.code, 403)
            # /api/action exists but only knows t3-pair; the stream route is
            # reachable (400 unknown-action, not the macOS 404 curtain).
            with self.assertRaises(urllib.error.HTTPError) as unknown:
                post("/api/action", {"action": "reboot"})
            self.assertEqual(unknown.exception.code, 400)
            with self.assertRaises(urllib.error.HTTPError) as stream:
                urllib.request.urlopen(origin + "/api/action-stream?action=nope", timeout=2)
            self.assertEqual(stream.exception.code, 400)
        finally:
            server.shutdown(); server.server_close(); thread.join(timeout=2)
        # Without the flag the same routes stay behind the 404 curtain (the
        # existing release-routes test covers /api/action; cover kill here).
        plain_server = plain.ThreadingHTTPServer(("127.0.0.1", 0), plain.Handler)
        plain_thread = threading.Thread(target=plain_server.serve_forever, daemon=True); plain_thread.start()
        try:
            with self.assertRaises(urllib.error.HTTPError) as off:
                urllib.request.urlopen(urllib.request.Request(
                    f"http://127.0.0.1:{plain_server.server_port}/api/session-kill",
                    data=b"{}", headers={"Content-Type": "application/json"}, method="POST"), timeout=2)
            self.assertEqual(off.exception.code, 404)
        finally:
            plain_server.shutdown(); plain_server.server_close(); plain_thread.join(timeout=2)

    def test_macos_agents_installer_status_and_updater_contract(self):
        install = (ROOT / "macos/install.sh").read_text()
        status_text = (ROOT / "macos/status.sh").read_text()
        uninstall = (ROOT / "macos/uninstall.sh").read_text()
        updater = (ROOT / "macos/updater.py").read_text()
        shell = (ROOT / "dashboard/static/index.html").read_text()
        self.assertIn("--agents", install)
        self.assertIn("agents=%s", install)
        self.assertIn("brew prerequisites", install)
        self.assertIn("set-path=/ http://127.0.0.1:4711", install)
        self.assertIn("set-path=/term http://127.0.0.1:4713", install)
        self.assertNotIn("sudo", install)
        self.assertIn("--agents requires the dashboard component", install)
        self.assertIn("refusing --agents", install)  # never seize a foreign / mount
        # Metadata is recorded before the health gates, so an aborted probe
        # can never orphan a bootstrapped-but-unrecorded agents layer.
        self.assertLess(install.index("agents=%s"), install.index("health(){"))
        self.assertIn("--max-time 5", install); self.assertIn("--max-time 5", status_text)
        self.assertIn("drift webterm bin/webterm", status_text)
        for tmpl, needle in (("io.gravedecay.t3", "--base-dir"), ("io.gravedecay.term", "webterm")):
            text = (ROOT / f"macos/LaunchAgents/{tmpl}.plist.tmpl").read_text()
            self.assertIn("127.0.0.1", text)
            self.assertIn("@JAIL@", text)
            self.assertIn(needle, text)
        # Converge-by-omission and uninstall only touch / when agents owned it.
        self.assertIn('elif [ "$OLD_AGENTS" = 1 ]', install)
        self.assertIn("io.gravedecay.t3", uninstall); self.assertIn("io.gravedecay.term", uninstall)
        self.assertIn("agents=", uninstall)
        # The unattended updater must restate the non-sticky opt-in.
        self.assertIn('"--agents"', updater)
        self.assertIn("io.gravedecay.t3 4711", status_text)
        self.assertIn("io.gravedecay.term 4713", status_text)
        self.assertIn("127.0.0.1:4711", status_text)
        self.assertIn("macagents", shell)
        with tempfile.TemporaryDirectory() as tmp:
            fake_bin = pathlib.Path(tmp, "bin"); fake_bin.mkdir()
            for name in ("uname", "tmux", "ttyd", "npm", "node", "t3"):
                p = fake_bin / name
                p.write_text("#!/bin/sh\necho Darwin\n" if name == "uname" else "#!/bin/sh\nexit 0\n")
                p.chmod(0o755)
            env = dict(os.environ, HOME=tmp, GRAVEDECAY_MAC_ROOT=str(pathlib.Path(tmp) / "root"),
                       PATH=f"{fake_bin}:/usr/bin:/bin")
            good = subprocess.run(["sh", str(ROOT / "macos/install.sh"), "--agents", "--no-serve", "--dry-run"],
                                  env=env, capture_output=True, text=True)
            self.assertEqual(good.returncode, 0, good.stderr)
            self.assertIn("io.gravedecay.t3.plist", good.stdout)
            self.assertIn("io.gravedecay.term.plist", good.stdout)
            for tool in ("tmux", "ttyd"):
                (fake_bin / tool).unlink()
                missing = subprocess.run(["sh", str(ROOT / "macos/install.sh"), "--agents", "--no-serve", "--dry-run"],
                                         env=env, capture_output=True, text=True)
                self.assertNotEqual(missing.returncode, 0)
                self.assertIn(tool, missing.stderr)
                (fake_bin / tool).write_text("#!/bin/sh\nexit 0\n"); (fake_bin / tool).chmod(0o755)

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
                self.assertEqual(denied.exception.code, 502)  # installed helper is absent in this source-only smoke
                time.sleep(.2)
                with urllib.request.urlopen(f"http://127.0.0.1:{net_port}/events", timeout=2) as stream:
                    event = stream.readline().decode() + stream.readline().decode()
                self.assertIn('"ifaces"', event)
            finally:
                for proc in procs: proc.terminate()
                for proc in procs: proc.wait(timeout=3)


if __name__ == "__main__":
    unittest.main()
