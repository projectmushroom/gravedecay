import importlib.util, socket, threading, unittest
from http.server import ThreadingHTTPServer
from pathlib import Path

spec = importlib.util.spec_from_file_location("gravenet", Path(__file__).parents[1] / "dashboard/gravenet.py")
NET = importlib.util.module_from_spec(spec); spec.loader.exec_module(NET)


class GravenetTests(unittest.TestCase):
    def test_binds_loopback_only(self):
        self.assertEqual(NET.BIND, "127.0.0.1")

    def test_event_stream_ends_when_the_sampler_drops_the_client(self):
        # The sampler is not running: no snapshot ever arrives, exactly as for a
        # client it dropped for a full queue. The stream must close, not hang.
        server = ThreadingHTTPServer(("127.0.0.1", 0), NET.Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            with socket.create_connection(server.server_address, timeout=10) as s:
                s.sendall(b"GET /events HTTP/1.1\r\nHost: x\r\n\r\n")
                out = b""
                while chunk := s.recv(4096): out += chunk
            self.assertIn(b"event: history", out)
            self.assertEqual(NET.SAMPLER.clients, set())
        finally:
            server.shutdown(); server.server_close()


if __name__ == "__main__":
    unittest.main()
