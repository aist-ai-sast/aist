from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import TestCase
from dojo.models import Product_Type

from aist.integrations.dast_config import DastConfigError, DastIntegrationConfig
from aist.models import (
    DastIntegrationState,
    DastIntegrationValidationState,
    Organization,
    OrgIntegration,
    OrgIntegrationType,
)


def _config_snapshot(**overrides):
    config = {
        "gateway_url": "https://dast-gateway.internal/",
        "ca_bundle": "-----BEGIN CERTIFICATE-----\npublic-ca\n-----END CERTIFICATE-----",
        "contract_major": 2,
        "integrator_public_id": "pub_aist",
        "server_fingerprint": "sha256:server-fingerprint",
    }
    config.update(overrides)
    return config


class DastIntegrationConfigTests(TestCase):
    def test_strict_snapshot_round_trip_is_canonical_and_defensive(self):
        source = _config_snapshot()
        config = DastIntegrationConfig.from_snapshot(source)
        source["gateway_url"] = "https://mutated.invalid"

        snapshot = config.to_snapshot()
        self.assertEqual(snapshot["gateway_url"], "https://dast-gateway.internal")
        snapshot["gateway_url"] = "https://also-mutated.invalid"
        self.assertEqual(config.gateway_url, "https://dast-gateway.internal")

    def test_unknown_missing_and_legacy_contract_fields_are_rejected(self):
        invalid_snapshots = (
            {**_config_snapshot(), "legacy_gateway_url": "https://legacy.invalid"},
            {key: value for key, value in _config_snapshot().items() if key != "server_fingerprint"},
            _config_snapshot(contract_major=1),
        )
        for snapshot in invalid_snapshots:
            with self.subTest(snapshot=snapshot), self.assertRaises(DastConfigError):
                DastIntegrationConfig.from_snapshot(snapshot)


class DastIntegrationModelTests(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(
            name="DAST model org",
            product_type=Product_Type.objects.create(name="DAST model PT"),
        )
        self.other_organization = Organization.objects.create(
            name="Other DAST model org",
            product_type=Product_Type.objects.create(name="Other DAST model PT"),
        )

    def _create_dast(self, *, organization=None, name="DAST", is_active=True):
        return OrgIntegration.objects.create(
            organization=organization or self.organization,
            integration_type=OrgIntegrationType.DAST,
            name=name,
            config=_config_snapshot(integrator_public_id=f"pub_{name.lower()}"),
            secret="pub_aist.secretvaluevaluevalue",  # noqa: S106
            is_active=is_active,
        )

    def test_only_one_active_dast_integration_per_organization(self):
        self._create_dast(name="Primary")
        with self.assertRaises(IntegrityError), transaction.atomic():
            self._create_dast(name="Secondary")

        self._create_dast(name="History", is_active=False)
        self._create_dast(organization=self.other_organization, name="Other org")

    def test_dast_config_is_validated_by_model_boundary(self):
        integration = OrgIntegration(
            organization=self.organization,
            integration_type=OrgIntegrationType.DAST,
            name="Invalid config",
            config={"gateway_url": "http://legacy.invalid"},
        )
        with self.assertRaises(ValidationError):
            integration.full_clean()

    def test_durable_validation_and_sync_state_requires_dast_integration(self):
        integration = self._create_dast(name="State")
        state = DastIntegrationState.objects.create(
            integration=integration,
            validation_state=DastIntegrationValidationState.READY,
            contract_version="2.0",
            capabilities_etag='"capabilities-v2"',
        )
        state.refresh_from_db()
        self.assertEqual(state.validation_state, DastIntegrationValidationState.READY)

        non_dast = OrgIntegration.objects.create(
            organization=self.organization,
            integration_type=OrgIntegrationType.GITHUB,
            name="GitHub",
        )
        invalid_state = DastIntegrationState(integration=non_dast)
        with self.assertRaises(ValidationError):
            invalid_state.full_clean()
