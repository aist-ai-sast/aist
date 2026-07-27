"""Tests for the versioned DAST integration validation boundary."""
from __future__ import annotations

from unittest.mock import patch

from django.test import TestCase
from dojo.models import Product_Type

from aist.integrations.dast import probe_dast_gateway
from aist.integrations.dast_gateway_client import DastGatewayClientError
from aist.models import (
    Organization,
    OrgIntegration,
    OrgIntegrationType,
)


def _dast_config(**overrides):
    config = {
        "gateway_url": "https://dast-gateway.internal",
        "ca_bundle": "",
        "contract_major": 2,
        "integrator_public_id": "pub_abc123",
        "server_fingerprint": "sha256:server-fingerprint",
    }
    config.update(overrides)
    return config


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
            config=_dast_config(),
        )

    @patch("aist.integrations.dast.scoped_dast_gateway_client")
    def test_probe_returns_valid_on_200(self, mock_client_context):
        valid, detail = probe_dast_gateway(self.integration)

        self.assertTrue(valid)
        self.assertNotIn(self.integration.secret, detail)
        mock_client_context.return_value.__enter__.return_value.ping.assert_called_once_with()

    @patch("aist.integrations.dast.scoped_dast_gateway_client")
    def test_probe_returns_redacted_token_rejection(self, mock_client_context):
        mock_client_context.return_value.__enter__.return_value.ping.side_effect = DastGatewayClientError(
            "TOKEN_REJECTED",
        )
        valid, detail = probe_dast_gateway(self.integration)

        self.assertFalse(valid)
        self.assertEqual(detail, "TOKEN_REJECTED")
        self.assertNotIn(self.integration.secret, detail)

    @patch("aist.integrations.dast.scoped_dast_gateway_client")
    def test_probe_unreachable_returns_invalid_with_reason(self, mock_client_context):
        mock_client_context.side_effect = DastGatewayClientError("GATEWAY_UNREACHABLE", retryable=True)
        valid, detail = probe_dast_gateway(self.integration)

        self.assertFalse(valid)
        self.assertEqual(detail, "GATEWAY_UNREACHABLE")
        self.assertNotIn(self.integration.secret, detail)

    @patch("aist.integrations.dast.scoped_dast_gateway_client")
    def test_probe_uses_integration_scoped_client(self, mock_client_context):
        probe_dast_gateway(self.integration)

        mock_client_context.assert_called_once_with(
            self.integration,
            execution_id=f"dast-probe-{self.integration.pk}",
        )

    def test_probe_missing_gateway_url_returns_invalid_without_network_call(self):
        self.integration.config = {}
        self.integration.save(update_fields=["config"])

        valid, detail = probe_dast_gateway(self.integration)

        self.assertFalse(valid)
        self.assertEqual(detail, "CONFIG_INVALID")
