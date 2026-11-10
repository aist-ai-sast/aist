"""Deploy a relocated stack with fake Docker processes, checking stop/migrate/start ordering."""
import json
import os
import subprocess
import tempfile
from pathlib import Path

from django.test import SimpleTestCase

from aist.test import PROJECT_ROOT
from aist.test.test_deployment_configuration import compose_fixture


class ProductionDeployScriptTests(SimpleTestCase):
    def _deploy(self, *, initializer_failure=False, stale_origin=False):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = compose_fixture()
            config["services"]["nginx"]["volumes"][0]["source"] = str(root)
            if stale_origin:
                config["services"]["celeryworker"]["environment"]["PUBLIC_BASE_URL"] = "https://old.example"
            for name in ("nginx.crt", "nginx.key"):
                (root / name).write_text("fixture pair")
            log = root / "commands.log"
            fake_docker = root / "docker"
            fake_docker.write_text(
                "#!/usr/bin/env python3\nimport json, os, sys\n"
                f"with open({str(log)!r}, 'a') as log: log.write(json.dumps(sys.argv[1:]) + '\\n')\n"
                "if sys.argv[1] == 'compose':\n"
                "    assert 'PUBLIC_BASE_URL' not in os.environ, 'shell leaked into Compose'\n"
                "if '--format' in sys.argv:\n"
                f"    print({json.dumps(config)!r})\n"
                "elif '--entrypoint' in sys.argv:\n"
                "    print('fixture-dast-connector:relocation')\n"
                "elif '--services' in sys.argv:\n"
                "    print('nginx\\nuwsgi\\npostgres\\nvalkey\\nceleryworker\\ncelerybeat\\nclaude-bridge')\n"
                f"elif {initializer_failure!r} and 'run' in sys.argv and 'initializer' in sys.argv: sys.exit(7)\n",
            )
            fake_docker.chmod(0o755)
            fake_curl = root / "curl"
            fake_curl.write_text(
                "#!/usr/bin/env python3\nimport json, sys\n"
                f"with open({str(log)!r}, 'a') as log: log.write(json.dumps(['curl', *sys.argv[1:]]) + '\\n')\n",
            )
            fake_curl.chmod(0o755)
            result = subprocess.run(
                ["/bin/bash", str(PROJECT_ROOT / "scripts/deploy-prod.sh")],
                env={**os.environ, "PROJECT_DIR": str(PROJECT_ROOT), "COMPOSE_ENV_FILE": "selected.env",
                     "PATH": str(root) + os.pathsep + os.environ["PATH"], "PUBLIC_BASE_URL": "https://stale-shell.example"},
                capture_output=True, text=True, check=False,
            )
            calls = [json.loads(line) for line in log.read_text().splitlines()]
            return result, calls

    def test_deployment_migrates_while_writers_are_stopped_and_verifies_current_origin(self):
        result, calls = self._deploy()
        self.assertEqual(result.returncode, 0, result.stderr)
        commands = [" ".join(call) for call in calls]
        stop = next(i for i, call in enumerate(commands) if "stop nginx celerybeat celeryworker" in call)
        migrate = next(i for i, call in enumerate(commands) if "run --rm --no-deps initializer" in call)
        start = next(i for i, call in enumerate(commands) if "up -d --no-build" in call)
        self.assertTrue(any("--tag fixture-dast-connector:relocation" in call for call in commands))
        self.assertLess(stop, migrate)
        self.assertLess(migrate, start)
        self.assertTrue(all("--env-file selected.env" in call for call in commands if call.startswith("compose ")))
        self.assertTrue(any("manage.py migrate --check" in call for call in commands))
        self.assertTrue(any("manage.py check --deploy --tag aist_execution" in call for call in commands))
        self.assertIn("--cacert", calls[-1])
        self.assertEqual(calls[-1][-1], "https://aist.internal:8443/")

    def test_failed_migration_does_not_start_application_or_schedulers(self):
        result, calls = self._deploy(initializer_failure=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(any("--no-build" in call for call in calls))

    def test_stale_worker_origin_fails_before_building_or_stopping_any_service(self):
        result, calls = self._deploy(stale_origin=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(len(calls), 1)
        self.assertIn("config", calls[0])
