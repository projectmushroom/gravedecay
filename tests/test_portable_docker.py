import importlib.util
import os
import pathlib
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]


def load_dashboard(env):
    old = dict(os.environ)
    os.environ.update(env)
    try:
        spec = importlib.util.spec_from_file_location("gravedecay_portable_probe",
                                                       ROOT / "dashboard/gravedecay.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        os.environ.clear()
        os.environ.update(old)


class PortableDockerContractTests(unittest.TestCase):
    def test_compose_has_one_loopback_gateway_and_separate_credential_volume(self):
        compose = (ROOT / "docker/portable/compose.yaml").read_text()
        self.assertIn('"127.0.0.1:${PORT:-4711}:8080"', compose)
        self.assertIn("- workspace:/workspace", compose)
        self.assertIn("- agent-home:/home/grave", compose)
        self.assertNotIn("docker.sock", compose)
        self.assertNotIn("\n    privileged:", compose)
        self.assertNotIn("network_mode: host", compose)
        self.assertNotIn("pid: host", compose)
        self.assertIn("cap_drop: [ALL]", compose)
        self.assertIn("read_only: true", compose)
        nginx = (ROOT / "docker/portable/nginx.conf").read_text()
        self.assertNotIn("proxy_params", nginx)
        self.assertIn("proxy_buffering off", nginx)
        self.assertIn("proxy_request_buffering off", nginx)
        self.assertIn("client_max_body_size 2g;", nginx)
        self.assertEqual(nginx.count("proxy_set_header Host $http_host;"), 4)
        self.assertIn("absolute_redirect off;", nginx)

    def test_portable_dashboard_has_no_host_actions_or_host_state(self):
        dash = load_dashboard({"GRAVEDECAY_PLATFORM": "container"})
        self.assertTrue(dash.PORTABLE)
        self.assertEqual(set(dash.ACTIONS), {"t3-pair"})
        dash.unit_state = lambda _: (_ for _ in ()).throw(AssertionError("host systemd read"))
        dash.collect_github = lambda: {"login": None, "prs": []}
        dash.collect_linear = lambda: {"configured": False, "issues": []}
        dash.collect_ci = lambda: {"rows": []}
        dash.collect_agent_usage = lambda: None
        dash.collect_repos = lambda: []
        dash.collect_inbox = lambda: []
        dash.collect_agent_history = lambda: []
        dash.collect_tmux = lambda: []
        state = dash.state({})
        self.assertEqual(state["platform"], "container")
        self.assertEqual(state["services"], [])
        self.assertEqual(state["journal"], [])
        self.assertEqual(state["docker"]["error"], "not managed by portable workspace")
        self.assertIsNone(dash.settings_response({})["notify"])
        shell = (ROOT / "dashboard/static/index.html").read_text()
        self.assertIn("activeTab='work'", shell)
        self.assertIn("!live&&!portableCompanion", shell)
        self.assertIn("body.portable .t3connect-only", shell)
        self.assertIn("body.portable #notify-head", shell)
        self.assertIn("t3connect-only", shell)
        self.assertIn("const appUrl=u=>portableCompanion?u:", shell)
        self.assertIn("!macosCompanion&&!portableCompanion", shell)
        self.assertNotIn("paintTabs", shell)
        self.assertIn("?` <a class=\"resume\"", shell)
        self.assertIn("`:''}</td></tr>`", shell)

    def test_pairing_scheme_is_local_http_or_forwarded_https(self):
        dash = load_dashboard({"GRAVEDECAY_PLATFORM": "container"})
        self.assertEqual(dash.public_scheme({}), "http")
        self.assertEqual(dash.public_scheme({"Tailscale-User-Login": "me@example.com"}), "https")
        self.assertEqual(dash.public_scheme({"X-Forwarded-Proto": "https"}), "https")

    def test_build_context_drops_local_credentials(self):
        ignore = (ROOT / ".dockerignore").read_text()
        self.assertIn(".git", ignore)
        self.assertIn("**/.env", ignore)
        self.assertIn("**/secrets/", ignore)
        for credential in ("**/.npmrc", "**/.netrc", "**/.git-credentials"):
            self.assertIn(credential, ignore)
        dockerfile = (ROOT / "docker/portable/Dockerfile").read_text()
        self.assertIn("/workspace /workspace/repos /workspace/agents/t3code /workspace/config /workspace/logs", dockerfile)
        self.assertNotIn("/workspace/{repos", dockerfile)
        self.assertIn(" gh ", dockerfile)
        self.assertNotIn("GRAVEDECAY_BIND", dockerfile)
        term_builder = (ROOT / "docker/portable/build-term.py").read_text()
        self.assertIn('assert "/*@" not in html', term_builder)
        self.assertIn("vendor/xterm-5.5.0.min.js", term_builder)
        start = (ROOT / "docker/portable/start.sh").read_text()
        self.assertIn("-I /opt/gravedecay/web/term/index.html", start)
        self.assertIn("http://127.0.0.1:4711/", dockerfile)
        self.assertIn("http://127.0.0.1:4712/healthz", dockerfile)
        self.assertIn("http://127.0.0.1:4713/term/", dockerfile)
        self.assertEqual(load_dashboard({"GRAVEDECAY_PLATFORM": "container"}).BIND_HOST, "0.0.0.0")
        self.assertEqual(load_dashboard({"GRAVEDECAY_PLATFORM": "linux"}).BIND_HOST, "127.0.0.1")


if __name__ == "__main__":
    unittest.main()
