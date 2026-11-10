"""Deployment validates one configured origin and never inherits shell application secrets."""
import os
from copy import deepcopy
from unittest.mock import patch

from django.test import SimpleTestCase, override_settings

from aist.utils.urls import build_callback_url
from scripts.deployment_config import compose_environment, deployment_settings


def compose_fixture():
    environment = {"DD_SITE_URL": "https://aist.internal:8443", "PUBLIC_BASE_URL": "https://aist.internal:8443"}
    return {
        "services": {
            **{name: {"environment": dict(environment)} for name in ("uwsgi", "celeryworker", "celerybeat", "initializer")},
            "nginx": {
                "environment": {"AIST_DOMAIN": "aist.internal", "USE_TLS": "true", "GENERATE_TLS_CERTIFICATE": "false"},
                "volumes": [
                    {"target": "/etc/nginx/ssl", "source": "/srv/aist/tls"},
                    {"target": "/var/www/certbot", "source": "/srv/aist/acme"},
                ],
                "ports": [{"target": 443, "published": "8443"}, {"target": 8080, "published": "8080"}],
            },
        },
        "volumes": {"defectdojo_postgres": {"driver_opts": {"device": "/srv/aist/db"}}},
        "x-aist-deployment": {
            "tls_mode": "selfsigned", "tls_valid_days": "365", "backup_dir": "/srv/backups/aist",
            "backup_retention_days": "30", "backup_on_calendar": "daily", "backup_remote": "",
            "timezone": "UTC", "firewall_manage": "false",
        },
    }


class DeploymentConfigurationTests(SimpleTestCase):
    def test_internal_hostname_and_nonstandard_tls_port_need_no_public_domain(self):
        config = compose_fixture()
        config["services"]["uwsgi"]["environment"]["DD_EMAIL_URL"] = "smtp://unchanged%40example.test:secret@relay"
        original = deepcopy(config)
        result = deployment_settings(config)
        self.assertEqual(result.origin, "https://aist.internal:8443")
        self.assertEqual(result.data_dir, "/srv/aist/db")
        self.assertNotIn("secret", str(result))
        self.assertEqual(config, original)

    def test_a_worker_with_a_stale_origin_stops_deployment(self):
        config = compose_fixture()
        config["services"]["celeryworker"]["environment"]["PUBLIC_BASE_URL"] = "https://old.example"
        with self.assertRaisesRegex(ValueError, "same origin"):
            deployment_settings(config)

    def test_ephemeral_tls_and_incorrect_ports_are_rejected(self):
        config = compose_fixture()
        config["services"]["nginx"]["environment"]["GENERATE_TLS_CERTIFICATE"] = "true"
        with self.assertRaisesRegex(ValueError, "persistent TLS"):
            deployment_settings(config)
        config = compose_fixture()
        config["services"]["nginx"]["ports"][0]["published"] = "443"
        with self.assertRaisesRegex(ValueError, "DD_TLS_PORT"):
            deployment_settings(config)

    def test_shell_application_values_cannot_override_the_selected_env_file(self):
        with patch.dict(os.environ, {"PUBLIC_BASE_URL": "https://old.example", "DD_EMAIL_URL": "secret", "DOCKER_HOST": "unix:///test.sock"}):
            environment = compose_environment()
        self.assertNotIn("PUBLIC_BASE_URL", environment)
        self.assertNotIn("DD_EMAIL_URL", environment)
        self.assertEqual(environment["DOCKER_HOST"], "unix:///test.sock")

    @override_settings(PUBLIC_BASE_URL="https://new.internal")
    def test_callback_urls_follow_the_configured_origin(self):
        self.assertTrue(build_callback_url("scan-1").startswith("https://new.internal/api/"))

    def test_malformed_compose_configuration_reports_a_configuration_error(self):
        for config in (None, [], {}, {"services": None}):
            with self.subTest(config=config), self.assertRaisesMessage(ValueError, "deployment field"):
                deployment_settings(config)
