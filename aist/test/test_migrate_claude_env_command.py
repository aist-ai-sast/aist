"""
Task 13 — migration command for stand-ups that already have the
legacy ``CLAUDE_CODE_OAUTH_TOKEN`` env var.

The command turns that env-only credential into a proper
``OrgIntegration(type=CLAUDE_CODE)`` row for the chosen organisation.
Idempotent — re-running does not create duplicates.
"""
from __future__ import annotations

from io import StringIO

from django.core.management import CommandError, call_command
from django.test import TestCase, override_settings
from dojo.models import Product_Type

from aist.models import Organization, OrgIntegration, OrgIntegrationType


class MigrateClaudeEnvCommandTests(TestCase):

    def setUp(self):
        self.org = Organization.objects.create(
            name="Migrate Org",
            product_type=Product_Type.objects.create(name="Migrate PT"),
        )

    def _run(self, env=None, **kwargs):
        out = StringIO()
        err = StringIO()
        with override_settings():
            import os  # noqa: PLC0415
            old = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN")
            try:
                if env is not None:
                    os.environ["CLAUDE_CODE_OAUTH_TOKEN"] = env
                else:
                    os.environ.pop("CLAUDE_CODE_OAUTH_TOKEN", None)
                call_command(
                    "migrate_claude_env_to_integration",
                    stdout=out,
                    stderr=err,
                    **kwargs,
                )
            finally:
                if old is not None:
                    os.environ["CLAUDE_CODE_OAUTH_TOKEN"] = old
                else:
                    os.environ.pop("CLAUDE_CODE_OAUTH_TOKEN", None)
        return out.getvalue(), err.getvalue()

    def test_creates_integration_from_env(self):
        self._run(env="sk-ant-oat01-legacy-token-from-env-1234567890", org=self.org.pk)
        qs = OrgIntegration.objects.filter(
            organization=self.org,
            integration_type=OrgIntegrationType.CLAUDE_CODE,
        )
        self.assertEqual(qs.count(), 1)
        integration = qs.first()
        self.assertEqual(integration.secret, "sk-ant-oat01-legacy-token-from-env-1234567890")
        self.assertEqual(integration.config.get("auth_mode"), "oauth")
        self.assertTrue(integration.is_active)

    def test_idempotent(self):
        self._run(env="sk-ant-oat01-legacy-token-from-env-1234567890", org=self.org.pk)
        # Second invocation must not raise, must not duplicate.
        out, _ = self._run(env="sk-ant-oat01-legacy-token-from-env-1234567890", org=self.org.pk)
        qs = OrgIntegration.objects.filter(
            organization=self.org,
            integration_type=OrgIntegrationType.CLAUDE_CODE,
        )
        self.assertEqual(qs.count(), 1)
        self.assertIn("already", out.lower())

    def test_missing_env_raises(self):
        with self.assertRaises(CommandError):
            self._run(env=None, org=self.org.pk)

    def test_missing_org_raises(self):
        # argparse-level error → CommandError.
        with self.assertRaises(CommandError):
            self._run(env="sk-ant-oat01-legacy-token-from-env-1234567890")

    def test_unknown_org_raises(self):
        with self.assertRaises(CommandError):
            self._run(env="sk-ant-oat01-legacy-token-from-env-1234567890", org=99999)
