"""The deployment helper synchronizes the checkout using the selected configuration."""
import json
import os
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from django.test import SimpleTestCase

from aist.test import PROJECT_ROOT


class PushToServerTests(SimpleTestCase):
    def test_sync_uses_configured_destination_and_missing_target_never_runs_rsync(self):
        helper = PROJECT_ROOT / "sast-combinator/utils/push_to_server.sh"
        for host in ("replacement.internal", ""):
            with self.subTest(host=host), TemporaryDirectory() as directory:
                root = Path(directory)
                log = root / "arguments.json"
                env_file = root / "selected.env"
                env_file.write_text(f"REMOTE_USER=deploy\nREMOTE_HOST={host}\nREMOTE_DIR=/srv/aist checkout\n")
                config = {"x-aist-sync": {"user": "deploy", "host": host, "directory": "/srv/aist checkout"}}
                docker = root / "docker"
                docker.write_text(
                    "#!/usr/bin/env python3\nimport os, sys\n"
                    f"assert sys.argv[1:] == ['compose', '--env-file', {str(env_file)!r}, '-f', 'docker-compose.yml', 'config', '--format', 'json']\n"
                    "assert 'REMOTE_HOST' not in os.environ\n"
                    f"print({json.dumps(config)!r})\n",
                )
                docker.chmod(0o755)
                executable = root / "rsync"
                executable.write_text(
                    "#!/usr/bin/env python3\nimport json, os, sys\n"
                    "with open(os.environ['RSYNC_TEST_LOG'], 'w') as stream: json.dump(sys.argv[1:], stream)\n",
                )
                executable.chmod(0o755)
                with patch.dict(os.environ, {
                    "PATH": str(root) + os.pathsep + os.environ["PATH"],
                    "RSYNC_TEST_LOG": str(log), "COMPOSE_ENV_FILE": str(env_file),
                    "COMPOSE_FILE": "docker-compose.yml", "REMOTE_HOST": "stale-shell.internal",
                }):
                    result = subprocess.run(
                        ["/bin/bash", str(helper)], cwd=root, capture_output=True, text=True, check=False,
                    )
                if not host:
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn("REMOTE_HOST", result.stderr)
                    self.assertFalse(log.exists())
                    continue
                self.assertEqual(result.returncode, 0, result.stderr)
                arguments = json.loads(log.read_text())
                self.assertIn("--delete", arguments)
                self.assertIn("media/", arguments)
                self.assertIn(".env*", arguments)
                self.assertEqual(Path(arguments[-2]).resolve(), PROJECT_ROOT)
                self.assertEqual(arguments[-1], "deploy@replacement.internal:'/srv/aist checkout/'")
