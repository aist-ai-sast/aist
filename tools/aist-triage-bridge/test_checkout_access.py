"""Run inside the bridge image as root; exercise its real startup against a host-owned checkout."""
import os
import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from bridge_startup import start_bridge, wait_until_serving

# A deploying user's ids on the host: never the image's claude user, never root.
HOST_UID = HOST_GID = 4321


class CheckoutAccessTests(unittest.TestCase):
    def _checkout(self, root: Path, *, env_mode: int) -> Path:
        checkout = root / "aist"
        skill = checkout / ".codex" / "skills" / "aist-finding-triage" / "SKILL.md"
        skill.parent.mkdir(parents=True)
        skill.write_text("# skill\n", encoding="utf-8")
        (checkout / ".env").write_text("COMPOSE_PROJECT_NAME=aist\n", encoding="utf-8")
        for path in [checkout, *checkout.rglob("*")]:
            os.chown(path, HOST_UID, HOST_GID)
            path.chmod(0o750 if path.is_dir() else 0o640)
        (checkout / ".env").chmod(env_mode)
        root.chmod(0o755)
        return checkout

    def test_claude_reads_a_checkout_owned_by_the_deploying_user(self):
        with TemporaryDirectory() as directory:
            checkout = self._checkout(Path(directory), env_mode=0o640)
            socket_path = "/run/claude-bridge/checkout-test.sock"
            bridge = start_bridge(socket_path, AIST_WORKING_DIR=str(checkout))
            try:
                self.assertTrue(wait_until_serving(bridge, socket_path), "Bridge did not start")
                skill = checkout / ".codex" / "skills" / "aist-finding-triage" / "SKILL.md"
                for path in (skill, checkout / ".env"):
                    read = subprocess.run(["/usr/sbin/gosu", "claude", "cat", str(path)], capture_output=True, check=False)
                    self.assertEqual(read.returncode, 0, read.stderr)
            finally:
                bridge.terminate()
                bridge.communicate(timeout=10)

    def test_a_restarted_bridge_follows_a_checkout_moved_to_another_group(self):
        """`docker restart` keeps the container's groups; the operator's chgrp must still take effect."""
        with TemporaryDirectory() as directory:
            checkout = self._checkout(Path(directory), env_mode=0o640)
            socket_path = "/run/claude-bridge/checkout-regroup.sock"
            for gid in (HOST_GID, HOST_GID + 1):
                for path in [checkout, *checkout.rglob("*")]:
                    os.chown(path, HOST_UID, gid)
                bridge = start_bridge(socket_path, AIST_WORKING_DIR=str(checkout))
                try:
                    self.assertTrue(wait_until_serving(bridge, socket_path), f"Bridge did not start for gid {gid}")
                finally:
                    bridge.terminate()
                    bridge.communicate(timeout=10)

    def test_bridge_refuses_to_start_when_claude_cannot_read_the_env_file(self):
        with TemporaryDirectory() as directory:
            checkout = self._checkout(Path(directory), env_mode=0o600)
            bridge = start_bridge("/run/claude-bridge/checkout-refused.sock", AIST_WORKING_DIR=str(checkout))
            _, stderr = bridge.communicate(timeout=30)
            self.assertNotEqual(bridge.returncode, 0)
            self.assertIn(f"cannot read {checkout}", stderr)


if __name__ == "__main__":
    unittest.main()
