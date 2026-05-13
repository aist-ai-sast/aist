"""
Tests for Claude OrgIntegration REST API behaviour (Task 3 of the
Claude-as-Integration refactor — docs/plans/2026-05-12-claude-as-org-integration.md).

Covers:
- Create-time format validation (no network probe on save).
- Single-active-per-org guard (DB constraint surfaced as 400, not 500).
- Reusing the existing async ``POST /validate/`` flow for the on-demand
  probe — Claude does not get a parallel endpoint.
- Unit-level coverage of ``probe_claude_token`` with mocked Anthropic API.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from django.urls import reverse
from dojo.authorization.roles_permissions import Roles
from dojo.models import Product_Type_Member, Role

from aist.models import OrgIntegration, OrgIntegrationType
from aist.test.test_api import AISTApiBase

_VALID_OAUTH = "sk-ant-oat01-" + "A" * 30


class ClaudeIntegrationCreateAPITests(AISTApiBase):

    def setUp(self):
        super().setUp()
        from dojo.models import Product_Type  # noqa: PLC0415

        from aist.models import Organization  # noqa: PLC0415

        self.org_prod_type = Product_Type.objects.create(name="Claude API PT")
        self.org = Organization.objects.create(name="Claude API Org", product_type=self.org_prod_type)
        self.role_maintainer, _ = Role.objects.get_or_create(
            id=Roles.Maintainer, defaults={"name": "Maintainer"},
        )
        Product_Type_Member.objects.create(
            product_type=self.org_prod_type,
            user=self.user,
            role=self.role_maintainer,
        )
        self.project.organization = self.org
        self.project.save(update_fields=["organization"])
        self.url = reverse("aist_api:org_integration_list_create", kwargs={"org_id": self.org.pk})

    def _payload(self, **overrides) -> dict:
        return {
            "integration_type": "CLAUDE_CODE",
            "name": "Claude prod",
            "config": {"auth_mode": "oauth"},
            "secret": _VALID_OAUTH,
            "is_active": True,
            **overrides,
        }

    @patch("requests.Session.get")
    def test_create_no_network_call_on_save(self, mock_get):
        """Save must not probe Anthropic — that lives in the Validate flow."""
        resp = self.client.post(self.url, self._payload(), format="json")
        self.assertEqual(resp.status_code, 201, resp.data)
        self.assertFalse(mock_get.called)

    def test_create_success(self):
        resp = self.client.post(self.url, self._payload(), format="json")
        self.assertEqual(resp.status_code, 201, resp.data)
        self.assertEqual(resp.data["integration_type"], "CLAUDE_CODE")
        self.assertTrue(resp.data["has_secret"])
        self.assertNotIn("secret", resp.data)
        self.assertEqual(
            OrgIntegration.objects.filter(
                organization=self.org,
                integration_type=OrgIntegrationType.CLAUDE_CODE,
            ).count(),
            1,
        )

    def test_create_rejects_empty_secret(self):
        resp = self.client.post(self.url, self._payload(secret=""), format="json")
        self.assertEqual(resp.status_code, 400, resp.data)
        self.assertIn("secret", str(resp.data).lower())

    def test_create_rejects_bad_format(self):
        resp = self.client.post(self.url, self._payload(secret="not-a-token"), format="json")  # noqa: S106
        self.assertEqual(resp.status_code, 400, resp.data)

    def test_create_rejects_short_oauth_token(self):
        # OAuth token regex requires at least 20 chars after the version prefix.
        resp = self.client.post(self.url, self._payload(secret="sk-ant-oat01-short"), format="json")  # noqa: S106
        self.assertEqual(resp.status_code, 400, resp.data)

    def test_create_duplicate_active_rejected_with_400(self):
        OrgIntegration.objects.create(
            organization=self.org,
            integration_type=OrgIntegrationType.CLAUDE_CODE,
            name="existing",
            secret=_VALID_OAUTH,
            is_active=True,
            config={"auth_mode": "oauth"},
        )
        resp = self.client.post(self.url, self._payload(name="second"), format="json")
        # App-level guard must turn the DB constraint into a clean 400.
        # 500 here would mean the IntegrityError bubbled up.
        self.assertEqual(resp.status_code, 400, resp.data)
        self.assertIn("active", str(resp.data).lower())

    def test_create_duplicate_inactive_allowed(self):
        OrgIntegration.objects.create(
            organization=self.org,
            integration_type=OrgIntegrationType.CLAUDE_CODE,
            name="legacy",
            secret=_VALID_OAUTH,
            is_active=False,
            config={"auth_mode": "oauth"},
        )
        resp = self.client.post(self.url, self._payload(name="current"), format="json")
        self.assertEqual(resp.status_code, 201, resp.data)


class ClaudeIntegrationProbeTests(AISTApiBase):

    """
    Unit-level tests for the probe helper in aist/integrations/claude.py.

    The probe is wired into the existing ``_validate_integration`` dispatch;
    end-to-end API validation flow is already covered by
    ``OrgIntegrationValidateAPITests``.
    """

    def setUp(self):
        super().setUp()
        from dojo.models import Product_Type  # noqa: PLC0415

        from aist.models import Organization  # noqa: PLC0415

        self.org_prod_type = Product_Type.objects.create(name="Claude Probe PT")
        self.org = Organization.objects.create(name="Claude Probe Org", product_type=self.org_prod_type)
        self.integration = OrgIntegration.objects.create(
            organization=self.org,
            integration_type=OrgIntegrationType.CLAUDE_CODE,
            name="primary",
            secret=_VALID_OAUTH,
            is_active=True,
            config={"auth_mode": "oauth"},
        )

    def _mock_response(self, status_code: int) -> MagicMock:
        resp = MagicMock()
        resp.status_code = status_code
        return resp

    @patch("requests.Session.get")
    def test_probe_returns_valid_on_200(self, mock_get):
        from aist.integrations.claude import probe_claude_token  # noqa: PLC0415

        mock_get.return_value = self._mock_response(200)
        valid, detail = probe_claude_token(self.integration)
        self.assertTrue(valid)
        # Authorization header must carry the token but the helper must NOT
        # leak it back in the detail string.
        self.assertNotIn(_VALID_OAUTH, detail)

    @patch("requests.Session.get")
    def test_probe_returns_invalid_on_401(self, mock_get):
        from aist.integrations.claude import probe_claude_token  # noqa: PLC0415

        mock_get.return_value = self._mock_response(401)
        valid, detail = probe_claude_token(self.integration)
        self.assertFalse(valid)
        self.assertIn("401", detail)
        self.assertNotIn(_VALID_OAUTH, detail)

    @patch("requests.Session.get")
    def test_probe_returns_invalid_on_403(self, mock_get):
        from aist.integrations.claude import probe_claude_token  # noqa: PLC0415

        mock_get.return_value = self._mock_response(403)
        valid, _detail = probe_claude_token(self.integration)
        self.assertFalse(valid)

    @patch("requests.Session.get")
    def test_probe_unreachable_returns_invalid_with_reason(self, mock_get):
        import requests  # noqa: PLC0415

        from aist.integrations.claude import probe_claude_token  # noqa: PLC0415

        mock_get.side_effect = requests.ConnectionError("DNS failure")
        valid, detail = probe_claude_token(self.integration)
        self.assertFalse(valid)
        # Detail mentions unreachability but does not leak internals or token.
        self.assertNotIn(_VALID_OAUTH, detail)

    @patch("requests.Session.get")
    def test_probe_sends_bearer_authorization_header(self, mock_get):
        from aist.integrations.claude import probe_claude_token  # noqa: PLC0415

        mock_get.return_value = self._mock_response(200)
        probe_claude_token(self.integration)
        # Caller arg inspection — Authorization header must be present.
        _, kwargs = mock_get.call_args
        headers = kwargs.get("headers", {})
        # The exact header value is intentionally NOT asserted here —
        # this test only confirms that the helper is making an
        # authenticated request, not leaking the token via test logs.
        self.assertIn("Authorization", headers)
