"""
Tests for ``aist.integrations.claude`` — the single concentrator of
Claude-integration knowledge introduced in Task 2 of
``docs/plans/2026-05-12-claude-as-org-integration.md``.

This module is the ONLY place in the codebase that knows the mapping
between an ``OrgIntegration(type=CLAUDE_CODE)`` and concrete env-var
names (``CLAUDE_CODE_OAUTH_TOKEN`` / ``ANTHROPIC_API_KEY``). The
architectural invariant I1 is enforced by Task 14's meta-test.
"""
from __future__ import annotations

from django.test import TestCase
from dojo.models import Product, Product_Type, SLA_Configuration
from pydantic import SecretStr

from aist.integrations.claude import claude_auth_env, redact_claude_secret
from aist.models import (
    AISTProject,
    Organization,
    OrgIntegration,
    OrgIntegrationType,
    ProjectIntegrationOverride,
)


class ClaudeAuthEnvTests(TestCase):

    def setUp(self):
        self.sla = SLA_Configuration.objects.create(name="SLA default")
        self.prod_type = Product_Type.objects.create(name="ClaudeAuth PT")
        self.org = Organization.objects.create(
            name="Claude Auth Org",
            product_type=self.prod_type,
        )
        product = Product.objects.create(
            name="Claude Auth Product",
            description="desc",
            prod_type=self.prod_type,
            sla_configuration_id=self.sla.id,
        )
        self.project = AISTProject.objects.create(
            product=product,
            supported_languages=["python"],
            compilable=False,
            profile={},
            organization=self.org,
        )

    def _make_integration(self, *, secret: str = "sk-ant-oat01-" + "x" * 30,
                          is_active: bool = True, config: dict | None = None,
                          ) -> OrgIntegration:
        return OrgIntegration.objects.create(
            organization=self.org,
            integration_type=OrgIntegrationType.CLAUDE_CODE,
            name="primary",
            secret=secret,
            is_active=is_active,
            config=config or {"auth_mode": "oauth"},
        )

    def test_no_integration_returns_empty_dict(self):
        result = claude_auth_env(self.project)
        self.assertEqual(result, {})

    def test_oauth_mode_returns_oauth_token_env(self):
        self._make_integration(config={"auth_mode": "oauth"})
        result = claude_auth_env(self.project)
        self.assertEqual(set(result.keys()), {"CLAUDE_CODE_OAUTH_TOKEN"})
        self.assertIsInstance(result["CLAUDE_CODE_OAUTH_TOKEN"], SecretStr)
        self.assertEqual(
            result["CLAUDE_CODE_OAUTH_TOKEN"].get_secret_value(),
            "sk-ant-oat01-" + "x" * 30,
        )

    def test_default_auth_mode_is_oauth(self):
        # config without explicit auth_mode → falls back to oauth so that
        # legacy/admin-created rows just work.
        self._make_integration(config={})
        result = claude_auth_env(self.project)
        self.assertIn("CLAUDE_CODE_OAUTH_TOKEN", result)

    def test_api_key_mode_returns_anthropic_api_key_env(self):
        # Forward-compat: backend supports both modes, UI starts OAuth-only
        # (see plan Q5). When/if UI exposes api_key, this resolution must
        # work without further changes.
        self._make_integration(
            secret="sk-ant-test123",  # noqa: S106
            config={"auth_mode": "api_key"},
        )
        result = claude_auth_env(self.project)
        self.assertEqual(set(result.keys()), {"ANTHROPIC_API_KEY"})
        self.assertEqual(
            result["ANTHROPIC_API_KEY"].get_secret_value(),
            "sk-ant-test123",
        )

    def test_empty_secret_returns_empty_dict(self):
        # An integration row with no secret is treated as not configured —
        # otherwise downstream would receive ``{"CLAUDE_CODE_OAUTH_TOKEN": ""}``
        # and the claude CLI would fail with a confusing auth error.
        self._make_integration(secret="")
        result = claude_auth_env(self.project)
        self.assertEqual(result, {})

    def test_inactive_integration_returns_empty_dict(self):
        self._make_integration(is_active=False)
        result = claude_auth_env(self.project)
        self.assertEqual(result, {})

    def test_unknown_auth_mode_returns_empty_dict(self):
        # Fail-closed: if config has a typo (e.g. "ouath"), don't fall
        # back to oauth silently — the operator should notice the
        # missing integration.
        self._make_integration(config={"auth_mode": "ouath"})
        result = claude_auth_env(self.project)
        self.assertEqual(result, {})

    def test_cross_org_override_falls_back_to_org_default(self):
        # Defence in depth: even if a malformed ``ProjectIntegrationOverride``
        # points at another org's CLAUDE_CODE integration, resolve_integration
        # must reject it and fall back to this org's default. This re-asserts
        # the existing protection in aist/integrations/resolver.py for the
        # Claude code path specifically (see plan I-section).
        own = self._make_integration()
        other_org = Organization.objects.create(
            name="Other Org",
            product_type=Product_Type.objects.create(name="Other PT"),
        )
        alien = OrgIntegration.objects.create(
            organization=other_org,
            integration_type=OrgIntegrationType.CLAUDE_CODE,
            name="alien",
            secret="sk-ant-oat01-alien-secret-value-zzzz",  # noqa: S106
            is_active=True,
            config={"auth_mode": "oauth"},
        )
        ProjectIntegrationOverride.objects.create(
            project=self.project,
            integration_type=OrgIntegrationType.CLAUDE_CODE,
            org_integration=alien,
        )

        result = claude_auth_env(self.project)

        # Must resolve to OWN integration, never alien's secret.
        self.assertEqual(
            result["CLAUDE_CODE_OAUTH_TOKEN"].get_secret_value(),
            own.secret,
        )
        self.assertNotIn("alien", result["CLAUDE_CODE_OAUTH_TOKEN"].get_secret_value())


class RedactClaudeSecretTests(TestCase):

    def test_redact_replaces_known_values(self):
        env = {"CLAUDE_CODE_OAUTH_TOKEN": SecretStr("sk-ant-oat01-super-secret")}
        text = "auth error: invalid token sk-ant-oat01-super-secret"
        redacted = redact_claude_secret(text, env)
        self.assertNotIn("sk-ant-oat01-super-secret", redacted)
        self.assertIn("***REDACTED***", redacted)

    def test_redact_with_empty_env_returns_input_unchanged(self):
        text = "no secret here"
        self.assertEqual(redact_claude_secret(text, {}), text)

    def test_redact_skips_empty_values(self):
        # SecretStr("") should not become a global wildcard replacement
        # (s.replace("", "X") explodes a string with X between every char).
        env = {"CLAUDE_CODE_OAUTH_TOKEN": SecretStr("")}
        self.assertEqual(redact_claude_secret("hello", env), "hello")
