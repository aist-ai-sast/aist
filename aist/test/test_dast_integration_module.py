"""
Tests for ``aist.integrations.dast`` — the single concentrator of DAST-integration knowledge
(the credential seam that lets a project's DAST analyzer reach the DAST integration gateway).

This module is the ONLY place in the codebase that knows the mapping between an
``OrgIntegration(type=DAST)`` and concrete env-var names (``DAST_GATEWAY_URL`` /
``DAST_INTEGRATOR_TOKEN``), mirroring ``aist.integrations.claude``'s single-concentrator pattern.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import requests
from django.test import TestCase
from dojo.models import Product, Product_Type, SLA_Configuration

from aist.integrations.dast import dast_env, probe_dast_gateway
from aist.models import (
    AISTProject,
    Organization,
    OrgIntegration,
    OrgIntegrationType,
    ProjectIntegrationOverride,
)


class DastEnvTests(TestCase):

    def setUp(self):
        self.sla = SLA_Configuration.objects.create(name="SLA default")
        self.prod_type = Product_Type.objects.create(name="DastAuth PT")
        self.org = Organization.objects.create(
            name="Dast Auth Org",
            product_type=self.prod_type,
        )
        product = Product.objects.create(
            name="Dast Auth Product",
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

    def _make_integration(self, *, secret: str = "pub_abc123.secretvaluevaluevalue",  # noqa: S107
                          is_active: bool = True, config: dict | None = None,
                          ) -> OrgIntegration:
        return OrgIntegration.objects.create(
            organization=self.org,
            integration_type=OrgIntegrationType.DAST,
            name="primary",
            secret=secret,
            is_active=is_active,
            config=config if config is not None else {"gateway_url": "https://dast-gateway.internal"},
        )

    def test_no_integration_returns_empty_dict(self):
        self.assertEqual(dast_env(self.project), {})

    def test_configured_integration_returns_both_env_vars(self):
        self._make_integration()
        result = dast_env(self.project)
        self.assertEqual(
            result,
            {
                "DAST_GATEWAY_URL": "https://dast-gateway.internal",
                "DAST_INTEGRATOR_TOKEN": "pub_abc123.secretvaluevaluevalue",
            },
        )

    def test_empty_secret_returns_empty_dict(self):
        # A half-configured integration (URL but no token) is treated as not
        # configured — otherwise the analyzer would get DAST_GATEWAY_URL with
        # no DAST_INTEGRATOR_TOKEN and fail with a confusing 401 instead of a
        # clear "DAST not configured" line.
        self._make_integration(secret="")
        self.assertEqual(dast_env(self.project), {})

    def test_missing_gateway_url_returns_empty_dict(self):
        self._make_integration(config={})
        self.assertEqual(dast_env(self.project), {})

    def test_inactive_integration_returns_empty_dict(self):
        self._make_integration(is_active=False)
        self.assertEqual(dast_env(self.project), {})

    def test_cross_org_override_falls_back_to_org_default(self):
        # Defence in depth: even if a malformed ProjectIntegrationOverride points at
        # another org's DAST integration, resolve_integration must reject it and fall
        # back to this org's default — re-asserts resolver.py's existing protection
        # for the DAST path specifically.
        own = self._make_integration()
        other_org = Organization.objects.create(
            name="Other Org",
            product_type=Product_Type.objects.create(name="Other PT"),
        )
        alien = OrgIntegration.objects.create(
            organization=other_org,
            integration_type=OrgIntegrationType.DAST,
            name="alien",
            secret="alien_pub.alien_secret_value_value",  # noqa: S106
            is_active=True,
            config={"gateway_url": "https://alien-gateway.example"},
        )
        ProjectIntegrationOverride.objects.create(
            project=self.project,
            integration_type=OrgIntegrationType.DAST,
            org_integration=alien,
        )

        result = dast_env(self.project)

        self.assertEqual(result["DAST_INTEGRATOR_TOKEN"], own.secret)
        self.assertEqual(result["DAST_GATEWAY_URL"], "https://dast-gateway.internal")


class DastGatewayProbeTests(TestCase):

    """
    Unit-level tests for ``probe_dast_gateway`` — the probe wired into the existing
    ``_validate_integration`` dispatch (aist/api/org_integrations.py) for the "Validate" button.
    """

    def setUp(self):
        self.org = Organization.objects.create(
            name="Dast Probe Org",
            product_type=Product_Type.objects.create(name="Dast Probe PT"),
        )
        self.integration = OrgIntegration.objects.create(
            organization=self.org,
            integration_type=OrgIntegrationType.DAST,
            name="primary",
            secret="pub_abc123.secretvaluevaluevalue",  # noqa: S106
            is_active=True,
            config={"gateway_url": "https://dast-gateway.internal"},
        )

    def _mock_response(self, status_code: int) -> MagicMock:
        resp = MagicMock()
        resp.status_code = status_code
        return resp

    @patch("requests.Session.get")
    def test_probe_returns_valid_on_200(self, mock_get):
        mock_get.return_value = self._mock_response(200)
        valid, detail = probe_dast_gateway(self.integration)
        self.assertTrue(valid)
        self.assertNotIn(self.integration.secret, detail)

    @patch("requests.Session.get")
    def test_probe_returns_invalid_on_401(self, mock_get):
        mock_get.return_value = self._mock_response(401)
        valid, detail = probe_dast_gateway(self.integration)
        self.assertFalse(valid)
        self.assertIn("401", detail)
        self.assertNotIn(self.integration.secret, detail)

    @patch("requests.Session.get")
    def test_probe_unreachable_returns_invalid_with_reason(self, mock_get):
        mock_get.side_effect = requests.ConnectionError("DNS failure")
        valid, detail = probe_dast_gateway(self.integration)
        self.assertFalse(valid)
        self.assertNotIn(self.integration.secret, detail)

    @patch("requests.Session.get")
    def test_probe_sends_bearer_authorization_header_to_ping_path(self, mock_get):
        mock_get.return_value = self._mock_response(200)
        probe_dast_gateway(self.integration)
        called_url = mock_get.call_args[0][0]
        self.assertEqual(called_url, "https://dast-gateway.internal/integrations/v1/ping")
        called_headers = mock_get.call_args.kwargs["headers"]
        self.assertEqual(called_headers, {"Authorization": f"Bearer {self.integration.secret}"})

    @patch("requests.Session.get")
    def test_probe_missing_gateway_url_returns_invalid_without_network_call(self, mock_get):
        self.integration.config = {}
        self.integration.save(update_fields=["config"])

        valid, detail = probe_dast_gateway(self.integration)

        self.assertFalse(valid)
        self.assertIn("gateway_url", detail)
        mock_get.assert_not_called()
