import os
import pathlib
import subprocess
import tempfile
import unittest

SCRIPT = pathlib.Path(__file__).parents[1] / "libexec/firewall-harden"


class FirewallHardenOrder(unittest.TestCase):
    """The default zone may only become drop after ssh is open and verified:
    switching first locks out a box with no console (EC2)."""

    def run_harden(self, query_rc):
        with tempfile.TemporaryDirectory() as tmp:
            bin_dir, log = pathlib.Path(tmp), pathlib.Path(tmp) / "calls"
            fakes = {
                "systemctl": "[ \"$1\" != is-active ]",   # enable succeeds; docker is not running
                "sshd": "echo 'port 2222'",
                "ip": "echo '2: eth0: <UP>'",
                "firewall-cmd": f'echo "$*" >> {log}\n'
                                f'case "$*" in *--query-port*) exit {query_rc};; --get-default-zone) echo public;; esac',
            }
            for name, body in fakes.items():
                path = bin_dir / name
                path.write_text("#!/bin/sh\n" + body + "\n")
                path.chmod(0o755)
            result = subprocess.run(["bash", str(SCRIPT)], capture_output=True, text=True,
                                    env={**os.environ, "PATH": f"{bin_dir}:/usr/bin:/bin"})
            return result.returncode, log.read_text().splitlines()

    def test_drop_zone_follows_verified_ssh_port(self):
        rc, calls = self.run_harden(query_rc=0)
        self.assertEqual(rc, 0)
        switch = calls.index("--set-default-zone=drop")
        queries = [i for i, call in enumerate(calls) if "--query-port=2222/tcp" in call]
        self.assertEqual(len(queries), 2)   # runtime and permanent
        self.assertLess(max(queries), switch)
        self.assertIn("--zone=drop --add-port=2222/tcp", calls[:switch])

    def test_failed_verification_never_switches_to_drop(self):
        rc, calls = self.run_harden(query_rc=1)
        self.assertEqual(rc, 1)
        self.assertNotIn("--set-default-zone=drop", calls)
        self.assertNotIn("--reload", calls)


if __name__ == "__main__":
    unittest.main()
