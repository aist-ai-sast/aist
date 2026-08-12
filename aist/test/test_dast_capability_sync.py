from contextlib import contextmanager
from unittest.mock import MagicMock, patch

from django.db import connection
from django.test import TransactionTestCase
from django.utils import timezone
from dojo.models import Product_Type

from aist.integrations.dast_capability_sync import (
    prepare_dast_capability_sync,
    run_dast_capability_sync,
)
from aist.integrations.dast_config import DastTargetSnapshot
from aist.integrations.dast_gateway_client import (
    DastGatewayClientError,
    DastTargetCatalog,
)
from aist.models import (
    DastIntegrationState,
    DastIntegrationValidationState,
    DastTarget,
    Organization,
    OrgIntegration,
    OrgIntegrationType,
)
from aist.services.dast_targets import refresh_dast_targets


def _target(provider_id="app", **overrides):
    payload = {
        "id": provider_id,
        "display_name": f"{provider_id} API",
        "contract_revision": "2.0",
        "capability_revision": "sha256:" + "b" * 64,
        "schema_digest": "sha256:" + "c" * 64,
        "parameter_schema": {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "type": "object",
            "additionalProperties": False,
            "properties": {},
        },
        "defaults": {},
        "repository_keys": ["source"],
        "launch_requirements": ["repository-trigger"],
        "autonomous_ready": True,
    }
    payload.update(overrides)
    return DastTargetSnapshot.from_snapshot(payload)


class DastCapabilitySyncTests(TransactionTestCase):

    def setUp(self):
        organization = Organization.objects.create(
            name="DAST sync org",
            product_type=Product_Type.objects.create(name="DAST sync PT"),
        )
        self.integration = OrgIntegration.objects.create(
            organization=organization,
            integration_type=OrgIntegrationType.DAST,
            name="DAST gateway",
            config={
                "gateway_url": "https://gateway.example",
                "ca_bundle": "",
                "contract_major": 2,
                "integrator_public_id": "pub_aist",
                "server_fingerprint": "sha256:server-fingerprint",
            },
            secret="pub_aist.secretvaluevaluevalue",  # noqa: S106
        )
        self.state = DastIntegrationState.objects.create(
            integration=self.integration,
            validation_state=DastIntegrationValidationState.READY,
            contract_version="2.0",
        )

    def _client_context(self, client):
        @contextmanager
        def factory(_integration, *, execution_id):
            self.assertFalse(connection.in_atomic_block)
            self.assertTrue(execution_id.startswith("dast-capability-sync-"))
            yield client

        return factory

    def test_success_atomically_adds_changes_and_marks_missing_targets_unavailable(self):
        old, removed = refresh_dast_targets(
            self.integration,
            (_target("app", display_name="Old name"), _target("removed")),
        )
        ticket = prepare_dast_capability_sync(self.integration)
        client = MagicMock()
        client.catalog.return_value = DastTargetCatalog(
            contract_version="2.0",
            etag="catalog-2",
            targets=(_target("app", display_name="New name"), _target("added")),
        )

        result = run_dast_capability_sync(ticket, client_context_factory=self._client_context(client))

        old.refresh_from_db()
        removed.refresh_from_db()
        self.state.refresh_from_db()
        self.assertEqual(result["etag"], "catalog-2")
        self.assertEqual(old.display_name, "New name")
        self.assertFalse(removed.is_available)
        self.assertTrue(DastTarget.objects.get(integration=self.integration, provider_id="added").is_available)
        self.assertEqual(self.state.capabilities_etag, "catalog-2")
        self.assertIsNotNone(self.state.capabilities_synced_at)

    @patch("aist.integrations.dast_capability_sync.refresh_dast_targets")
    def test_304_and_unchanged_etag_do_not_rewrite_targets(self, mock_refresh):
        self.state.capabilities_etag = "catalog-1"
        self.state.save(update_fields=["capabilities_etag"])
        for catalog in (
            DastTargetCatalog(contract_version="", etag="catalog-1", targets=(), not_modified=True),
            DastTargetCatalog(contract_version="2.0", etag="catalog-1", targets=(_target(),)),
        ):
            ticket = prepare_dast_capability_sync(self.integration)
            client = MagicMock()
            client.catalog.return_value = catalog

            run_dast_capability_sync(ticket, client_context_factory=self._client_context(client))

        mock_refresh.assert_not_called()

    def test_invalid_catalog_keeps_last_known_good_targets_and_etag(self):
        target = refresh_dast_targets(self.integration, (_target(),))[0]
        self.state.capabilities_etag = "last-known-good"
        self.state.capabilities_synced_at = timezone.now()
        self.state.save(update_fields=["capabilities_etag", "capabilities_synced_at"])
        ticket = prepare_dast_capability_sync(self.integration)
        client = MagicMock()
        client.catalog.side_effect = DastGatewayClientError("CATALOG_INVALID")

        result = run_dast_capability_sync(ticket, client_context_factory=self._client_context(client))

        self.state.refresh_from_db()
        target.refresh_from_db()
        self.assertEqual(result["error_code"], "CATALOG_INVALID")
        self.assertEqual(self.state.capabilities_etag, "last-known-good")
        self.assertTrue(target.is_available)

    def test_stale_sync_cannot_replace_catalog(self):
        ticket = prepare_dast_capability_sync(self.integration)
        client = MagicMock()

        def supersede_during_catalog(**_kwargs):
            prepare_dast_capability_sync(self.integration)
            return DastTargetCatalog(contract_version="2.0", etag="stale", targets=(_target(),))

        client.catalog.side_effect = supersede_during_catalog

        result = run_dast_capability_sync(ticket, client_context_factory=self._client_context(client))

        self.state.refresh_from_db()
        self.assertTrue(result["stale"])
        self.assertEqual(self.state.capabilities_etag, "")
        self.assertFalse(DastTarget.objects.filter(integration=self.integration).exists())
