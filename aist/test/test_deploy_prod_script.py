from pathlib import Path

from django.test import SimpleTestCase

PROJECT_ROOT = Path(__file__).resolve().parents[2]


class ProductionDeployScriptTests(SimpleTestCase):
    def test_one_command_deploy_builds_connector_before_existing_application_services(self):
        script = (PROJECT_ROOT / "scripts" / "deploy-prod.sh").read_text(encoding="utf-8")

        connector_build = script.index("docker build \\\n")
        application_start = script.index('docker compose --env-file "${COMPOSE_ENV_FILE}" up -d --build')
        self.assertLess(connector_build, application_start)
        self.assertIn('DAST_CONNECTOR_IMAGE="${DAST_CONNECTOR_IMAGE:-aist-dast-connector:v2}"', script)
        self.assertNotIn("dastworker", script.lower())
        self.assertNotIn("dast_queue", script.lower())

    def test_deploy_checks_required_services_migrations_and_generic_execution_runtime(self):
        script = (PROJECT_ROOT / "scripts" / "deploy-prod.sh").read_text(encoding="utf-8")

        self.assertIn("required_services=(nginx uwsgi postgres valkey celeryworker celerybeat)", script)
        self.assertIn("python3 manage.py migrate --check", script)
        self.assertIn("python3 manage.py check --deploy --tag aist_execution", script)
