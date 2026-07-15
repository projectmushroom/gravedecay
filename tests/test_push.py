import base64
import importlib.util
import json
import os
import pathlib
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

try:
    from cryptography.hazmat.primitives.asymmetric import ec
    HAVE_CRYPTO = True
except ImportError:
    HAVE_CRYPTO = False

ROOT = pathlib.Path(__file__).resolve().parents[1]


def b64u(s):
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def load_dashboard(env):
    """Fresh gravedecay module under a patched environment (same pattern as
    test_dashboard.py — module config is read from os.environ at import)."""
    old = dict(os.environ)
    os.environ.update(env)
    try:
        spec = importlib.util.spec_from_file_location(
            "gravedecay_push_probe", ROOT / "dashboard/gravedecay.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        os.environ.clear()
        os.environ.update(old)


# RFC 8291 Appendix A — the complete Web Push encryption example. If our
# aes128gcm implementation drifts from the spec, real push services reject or
# devices fail to decrypt, silently — this vector is the ground truth.
RFC8291 = {
    "plaintext": b"When I grow up, I want to be a watermelon",
    "as_private": "yfWPiYE-n46HLnH0KqZOF1fJJU3MYrct3AELtAQ-oRw",
    "ua_public": "BCVxsr7N_eNgVRqvHtD0zTZsEc6-VV-JvLexhqUzORcxaOzi6-AYWXvTBHm4bjyPjs7Vd8pZGH6SRpkNtoIAiw4",
    "auth": "BTBZMqHH6r4Tts7J_aSIgg",
    "salt": "DGv6ra1nlYgDCS1FRnbzlw",
    "message": "DGv6ra1nlYgDCS1FRnbzlwAAEABBBP4z9KsN6nGRTbVYI_c7VJSPQTBtkgcy27mlml"
               "MoZIIgDll6e3vCYLocInmYWAmS6TlzAC8wEqKK6PBru3jl7A_yl95bQpu6cVPTpK4"
               "Mqgkf1CXztLVBSt2Ks3oZwbuwXPXLWyouBWLVWGNWQexSgSxsj_Qulcy4a-fN",
}

# A real P-256 point (the RFC's UA key) so subscription validation passes.
VALID_SUB = {
    "endpoint": "https://push.example.net/send/abc123",
    "keys": {"p256dh": RFC8291["ua_public"], "auth": RFC8291["auth"]},
}


class PushModuleTests(unittest.TestCase):
    def test_crypto_never_silently_skips_in_ci(self):
        # The RFC 8291 vector must actually run in CI — a missing dependency
        # there would otherwise skip the only ground-truth check on the crypto.
        if os.environ.get("CI"):
            self.assertTrue(HAVE_CRYPTO, "CI must pip install cryptography")

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        os.makedirs(os.path.join(self.tmp.name, "config", "secrets"))
        self.dash = load_dashboard({
            "GRAVE_ROOT": self.tmp.name,
            "GRAVE_CONF": os.path.join(self.tmp.name, "grave.conf"),
        })

    @unittest.skipUnless(HAVE_CRYPTO, "python3-cryptography not installed")
    def test_rfc8291_appendix_a_vector(self):
        eph = ec.derive_private_key(
            int.from_bytes(b64u(RFC8291["as_private"]), "big"), ec.SECP256R1())
        out = self.dash._webpush_encrypt(
            b64u(RFC8291["ua_public"]), b64u(RFC8291["auth"]),
            RFC8291["plaintext"], _salt=b64u(RFC8291["salt"]), _eph=eph)
        self.assertEqual(self.dash._b64u(out), RFC8291["message"])

    @unittest.skipUnless(HAVE_CRYPTO, "python3-cryptography not installed")
    def test_vapid_key_created_private_and_stable(self):
        key1 = self.dash.vapid_public_b64()
        self.assertEqual(len(b64u(key1)), 65)
        mode = oct(os.stat(os.path.join(
            self.tmp.name, "config", "secrets", "vapid.pem")).st_mode & 0o777)
        self.assertEqual(mode, "0o600")
        self.assertEqual(self.dash.vapid_public_b64(), key1)  # stable across calls

    @unittest.skipUnless(HAVE_CRYPTO, "python3-cryptography not installed")
    def test_vapid_jwt_signature_verifies(self):
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature
        auth = self.dash._vapid_auth("https://push.example.net/send/xyz")
        self.assertTrue(auth.startswith("vapid t="))
        token = auth.split("t=", 1)[1].split(",", 1)[0]
        head, body, sig = token.split(".")
        claims = json.loads(b64u(body))
        self.assertEqual(claims["aud"], "https://push.example.net")
        self.assertEqual(json.loads(b64u(head))["alg"], "ES256")
        raw = b64u(sig)
        der = encode_dss_signature(
            int.from_bytes(raw[:32], "big"), int.from_bytes(raw[32:], "big"))
        pub = self.dash.vapid_private_key().public_key()
        pub.verify(der, f"{head}.{body}".encode(), ec.ECDSA(hashes.SHA256()))  # raises on mismatch

    def test_subscribe_validates_and_stores_privately(self):
        res = self.dash.push_subscribe({"subscription": VALID_SUB, "label": "iPhone <b>x</b>"})
        self.assertTrue(res["ok"])
        mode = oct(os.stat(self.dash.PUSH_SUBS_PATH).st_mode & 0o777)
        self.assertEqual(mode, "0o600")
        subs = self.dash._load_push_subs()
        self.assertEqual(len(subs), 1)
        self.assertNotIn("<", subs[0]["label"])  # markup stripped from labels

    def test_subscribe_rejects_bad_endpoints_and_keys(self):
        for sub in (
            {"endpoint": "http://plain.example/x", "keys": VALID_SUB["keys"]},
            {"endpoint": "file:///etc/passwd", "keys": VALID_SUB["keys"]},
            {"endpoint": VALID_SUB["endpoint"],
             "keys": {"p256dh": "AAAA", "auth": RFC8291["auth"]}},
            {"endpoint": VALID_SUB["endpoint"],
             "keys": {"p256dh": RFC8291["ua_public"], "auth": "AAAA"}},
        ):
            self.assertFalse(self.dash.push_subscribe({"subscription": sub})["ok"], sub)
        self.assertEqual(self.dash._load_push_subs(), [])

    def test_resubscribe_dedupes_by_endpoint(self):
        self.dash.push_subscribe({"subscription": VALID_SUB, "label": "old"})
        self.dash.push_subscribe({"subscription": VALID_SUB, "label": "new"})
        subs = self.dash._load_push_subs()
        self.assertEqual([s["label"] for s in subs], ["new"])

    def test_unsubscribe_by_opaque_id(self):
        res = self.dash.push_subscribe({"subscription": VALID_SUB})
        self.assertTrue(self.dash.push_unsubscribe({"id": res["id"]})["removed"] == 1)
        self.assertEqual(self.dash._load_push_subs(), [])

    def test_push_send_without_devices_reports_failure(self):
        res = self.dash.push_send({"title": "t"})
        self.assertFalse(res["ok"])

    def test_notify_events_precedence(self):
        # default: all classes
        self.assertEqual(self.dash.notify_events(), self.dash.NOTIFY_CLASSES)
        # grave.conf value is the fallback…
        with open(os.path.join(self.tmp.name, "grave.conf"), "w") as f:
            f.write('NOTIFY_EVENTS="doctor bogus-class"\n')
        self.assertEqual(self.dash.notify_events(), ["doctor"])
        # …and the ⚙️ override file wins (muting everything is expressible)
        self.dash.save_notify_events(["bell", "nonsense"])
        self.assertEqual(self.dash.notify_events(), ["bell"])
        self.dash.save_notify_events([])
        self.assertEqual(self.dash.notify_events(), [])

    def test_save_ntfy_keeps_existing_values_and_validates(self):
        self.dash.save_ntfy("https://ntfy.example/topic", "tk_abc")
        self.dash.save_ntfy(None, "tk_new")     # url kept, token replaced
        vals = self.dash._read_env_file(self.dash.NOTIFY_ENV)
        self.assertEqual(vals["NTFY_URL"], "https://ntfy.example/topic")
        self.assertEqual(vals["NTFY_TOKEN"], "tk_new")
        mode = oct(os.stat(self.dash.NOTIFY_ENV).st_mode & 0o777)
        self.assertEqual(mode, "0o600")
        with self.assertRaises(ValueError):
            self.dash.save_ntfy("javascript:alert(1)")
        with self.assertRaises(ValueError):
            self.dash.save_ntfy("https://x.example/t", 'evil"\ntoken')
        self.dash.save_ntfy(clear=True)
        self.assertFalse(self.dash.ntfy_configured())


class PushEndpointTests(unittest.TestCase):
    """Drive the real handler over HTTP, test_dashboard.py style."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        os.makedirs(os.path.join(cls.tmp.name, "config", "secrets"))
        cls.dash = load_dashboard({
            "GRAVE_ROOT": cls.tmp.name,
            "GRAVE_CONF": os.path.join(cls.tmp.name, "grave.conf"),
        })
        cls.server = cls.dash.ThreadingHTTPServer(("127.0.0.1", 0), cls.dash.Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.origin = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)
        cls.tmp.cleanup()

    def post(self, path, data):
        return urllib.request.urlopen(urllib.request.Request(
            self.origin + path, data=json.dumps(data).encode(),
            headers={"Content-Type": "application/json"}, method="POST"), timeout=5)

    @unittest.skipUnless(HAVE_CRYPTO, "python3-cryptography not installed")
    def test_push_key_endpoint_serves_vapid_public(self):
        with urllib.request.urlopen(self.origin + "/api/push-key", timeout=5) as r:
            j = json.load(r)
        self.assertTrue(j["ok"])
        self.assertEqual(len(b64u(j["key"])), 65)

    def test_subscribe_state_unsubscribe_roundtrip(self):
        with self.post("/api/push-subscribe",
                       {"subscription": VALID_SUB, "label": "test tablet"}) as r:
            j = json.load(r)
        self.assertTrue(j["ok"])
        with urllib.request.urlopen(self.origin + "/api/state", timeout=5) as r:
            devices = json.load(r)["notify"]["push"]["devices"]
        self.assertEqual([d["label"] for d in devices], ["test tablet"])
        self.assertNotIn("endpoint", devices[0])  # capability URL never leaves the box
        with self.post("/api/push-unsubscribe", {"id": j["id"]}) as r:
            self.assertTrue(json.load(r)["ok"])
        with urllib.request.urlopen(self.origin + "/api/state", timeout=5) as r:
            self.assertEqual(json.load(r)["notify"]["push"]["devices"], [])

    def test_push_send_with_no_devices_is_502(self):
        # grave's push leg relies on curl -f seeing a non-2xx for "nothing sent"
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.post("/api/push-send", {"title": "t", "body": "b"})
        self.assertEqual(ctx.exception.code, 502)

    def test_settings_saves_notify_prefs(self):
        with self.post("/api/settings", {
                "ntfy_url": "https://ntfy.example/box",
                "notify_events": ["bell", "doctor", "junk"]}) as r:
            j = json.load(r)
        self.assertTrue(j["ok"])
        self.assertTrue(j["notify"]["ntfy"])
        self.assertEqual(j["notify"]["events"], ["bell", "doctor"])
        with self.post("/api/settings", {"ntfy_clear": True}) as r:
            self.assertFalse(json.load(r)["notify"]["ntfy"])

    def test_settings_rejects_bad_ntfy_url(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.post("/api/settings", {"ntfy_url": "javascript:alert(1)"})
        self.assertEqual(ctx.exception.code, 400)


class ServiceWorkerContractTests(unittest.TestCase):
    def test_both_worker_copies_handle_push_and_click(self):
        # static/sw.js is the served asset; the SERVICE_WORKER constant is the
        # embedded fallback for interrupted upgrades — they must not drift.
        static = (ROOT / "dashboard/static/sw.js").read_text()
        embedded = (ROOT / "dashboard/gravedecay.py").read_text()
        for source, name in ((static, "static/sw.js"), (embedded, "SERVICE_WORKER")):
            self.assertIn("addEventListener('push'", source, name)
            self.assertIn("addEventListener('notificationclick'", source, name)
            self.assertIn("showNotification", source, name)
            self.assertIn("openWindow", source, name)


if __name__ == "__main__":
    unittest.main()
