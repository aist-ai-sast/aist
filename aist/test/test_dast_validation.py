from contextlib import contextmanager
from unittest.mock import MagicMock

from django.db import connection
from django.test import TransactionTestCase
from dojo.models import Product_Type

from aist.integrations.dast_gateway_client import DastGatewayClientError, DastGatewayPing
from aist.integrations.dast_validation import (
    mark_dast_validation_pending,
    prepare_dast_validation,
    run_dast_validation,
)
from aist.models import (
    DastIntegrationValidationState,
    Organization,
    OrgIntegration,
    OrgIntegrationType,
)


def _config():
    return {
        "gateway_url": "https://gateway.example",
        "ca_bundle": "",
        "contract_major": 2,
        "integrator_public_id": "pub_aist",
        "server_fingerprint": "sha256:server-fingerprint",
    }


class DastValidationTests(TransactionTestCase):

    def setUp(self):
        organization = Organization.objects.create(
            name="DAST validation org",
            product_type=Product_Type.objects.create(name="DAST validation PT"),
        )
        self.integration = OrgIntegration.objects.create(
            organization=organization,
            integration_type=OrgIntegrationType.DAST,
            name="DAST gateway",
            config=_config(),
            secret="pub_aist.secretvaluevaluevalue",  # noqa: S106
        )

    def _client_context(self, client):
        @contextmanager
        def factory(_integration, *, execution_id):
            self.assertFalse(connection.in_atomic_block)
            self.assertTrue(execution_id.startswith("dast-validation-"))
            yield client

        return factory

    def test_success_is_persisted_durably_and_duplicate_delivery_is_idempotent(self):
        ticket = prepare_dast_validation(self.integration)
        client = MagicMock()
        client.ping.return_value = DastGatewayPing(
            contract_version="2.0",
            gateway_version="2026.7",
            status="ok",
        )

        result = run_dast_validation(ticket, client_context_factory=self._client_context(client))
        duplicate = run_dast_validation(ticket, client_context_factory=self._client_context(client))

        state = self.integration.dast_state
        state.refresh_from_db()
        self.assertEqual(result["state"], DastIntegrationValidationState.READY)
        self.assertEqual(state.validation_state, DastIntegrationValidationState.READY)
        self.assertEqual(state.contract_version, "2.0")
        self.assertIsNotNone(state.validated_at)
        self.assertTrue(duplicate["stale"])
        client.ping.assert_called_once_with()

    def test_redacted_client_failure_is_persisted(self):
        ticket = prepare_dast_validation(self.integration)
        client = MagicMock()
        client.ping.side_effect = DastGatewayClientError("TOKEN_REJECTED")

        result = run_dast_validation(ticket, client_context_factory=self._client_context(client))

        state = self.integration.dast_state
        state.refresh_from_db()
        self.assertEqual(result["error_code"], "TOKEN_REJECTED")
        self.assertEqual(state.validation_state, DastIntegrationValidationState.INVALID)
        self.assertEqual(state.validation_error_code, "TOKEN_REJECTED")

    def test_stale_task_cannot_overwrite_changed_connection_state(self):
        ticket = prepare_dast_validation(self.integration)
        client = MagicMock()

        def change_connection_during_ping():
            mark_dast_validation_pending(self.integration)
            return DastGatewayPing(contract_version="2.0", gateway_version="2026.7", status="ok")

        client.ping.side_effect = change_connection_during_ping

        result = run_dast_validation(ticket, client_context_factory=self._client_context(client))

        state = self.integration.dast_state
        state.refresh_from_db()
        self.assertTrue(result["stale"])
        self.assertEqual(state.validation_state, DastIntegrationValidationState.PENDING_VALIDATION)
        self.assertEqual(state.contract_version, "")

    def test_inactive_integration_fails_without_creating_network_context(self):
        self.integration.is_active = False
        self.integration.save(update_fields=["is_active"])
        ticket = prepare_dast_validation(self.integration)
        context_factory = MagicMock()

        result = run_dast_validation(ticket, client_context_factory=context_factory)

        self.assertEqual(result["error_code"], "INTEGRATION_DISABLED")
        context_factory.assert_not_called()
