from datetime import timedelta

from django.test import TestCase
from django.utils import timezone
from dojo.models import Product, Product_Type, SLA_Configuration

from aist.integrations.dast_config import DastTargetSnapshot
from aist.integrations.dast_readiness import DastReadinessCode, check_dast_binding_readiness
from aist.models import (
    AISTProject,
    DastIntegrationState,
    DastIntegrationValidationState,
    DastProjectBinding,
    Organization,
    OrgIntegration,
    OrgIntegrationType,
    OrgIntegrationVPNSecret,
)
from aist.services.dast_targets import refresh_dast_targets


def _integration_config(gateway_url: str = "https://gateway.example") -> dict:
    return {
        "gateway_url": gateway_url,
        "ca_bundle": "",
        "contract_major": 2,
        "integrator_public_id": "readiness-public-id",
        "server_fingerprint": "sha256:readiness-fingerprint",
    }


def _target_snapshot() -> DastTargetSnapshot:
    return DastTargetSnapshot.from_snapshot({
        "id": "source-api",
        "display_name": "Source API",
        "contract_revision": "2.0",
        "capability_revision": "capability-1",
        "schema_digest": "schema-1",
        "parameter_schema": {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "type": "object",
            "additionalProperties": False,
            "properties": {},
        },
        "defaults": {},
        "repository_keys": ["source"],
        "autonomous_ready": True,
    })


class DastReadinessTests(TestCase):

    def setUp(self):
        self.now = timezone.now()
        self.product_type = Product_Type.objects.create(name="DAST readiness PT")
        self.organization = Organization.objects.create(name="DAST readiness org", product_type=self.product_type)
        self.product = Product.objects.create(
            name="DAST readiness product",
            description="",
            prod_type=self.product_type,
            sla_configuration=SLA_Configuration.objects.create(name="DAST readiness SLA"),
        )
        self.project = AISTProject.objects.create(product=self.product)
        self.integration = OrgIntegration.objects.create(
            organization=self.organization,
            integration_type=OrgIntegrationType.DAST,
            name="DAST readiness",
            config=_integration_config(),
            secret="one-time-token-exchanged-for-runtime-token",  # noqa: S106 -- test fixture
            is_active=True,
        )
        self.state = DastIntegrationState.objects.create(
            integration=self.integration,
            validation_state=DastIntegrationValidationState.READY,
            validated_at=self.now,
            contract_version="2.0",
            capabilities_etag="catalog-1",
            capabilities_synced_at=self.now,
        )
        self.target = refresh_dast_targets(
            self.integration,
            (_target_snapshot(),),
            seen_at=self.now,
        )[0]
        self.binding = DastProjectBinding.objects.create(
            project=self.project,
            target=self.target,
            source_repo_key="source",
            enabled=True,
            parameter_snapshot={},
            autonomous_enabled=True,
        )

    def _fresh_binding(self) -> DastProjectBinding:
        return DastProjectBinding.objects.select_related(
            "target__integration__dast_state",
            "target__integration__vpn_integration__vpn_secret",
        ).get(pk=self.binding.pk)

    def _result_codes(self) -> set[DastReadinessCode]:
        return {issue.code for issue in check_dast_binding_readiness(self._fresh_binding(), now=self.now).issues}

    def test_complete_public_onboarding_is_ready_and_snapshot_is_secret_free(self):
        result = check_dast_binding_readiness(self._fresh_binding(), now=self.now)

        self.assertTrue(result.ready)
        self.assertEqual(result.issues, ())
        snapshot = result.to_snapshot()
        self.assertEqual(snapshot["ready"], True)
        self.assertEqual(snapshot["issues"], [])
        self.assertNotIn(self.integration.secret, str(snapshot))

    def test_each_durable_onboarding_prerequisite_has_a_stable_reason(self):
        binding = self._fresh_binding()
        cases = (
            (
                DastReadinessCode.INTEGRATION_TYPE_INVALID,
                binding.target.integration,
                "integration_type",
                OrgIntegrationType.GITLAB,
            ),
            (
                DastReadinessCode.INTEGRATION_INACTIVE,
                binding.target.integration,
                "is_active",
                False,
            ),
            (
                DastReadinessCode.INTEGRATION_TOKEN_MISSING,
                binding.target.integration,
                "secret",
                "",
            ),
            (
                DastReadinessCode.INTEGRATION_CONFIG_INVALID,
                binding.target.integration,
                "config",
                {},
            ),
            (
                DastReadinessCode.VALIDATION_NOT_READY,
                binding.target.integration.dast_state,
                "validation_state",
                DastIntegrationValidationState.INVALID,
            ),
            (
                DastReadinessCode.CONTRACT_INCOMPATIBLE,
                binding.target.integration.dast_state,
                "contract_version",
                "3.0",
            ),
            (
                DastReadinessCode.CATALOG_NOT_SYNCED,
                binding.target.integration.dast_state,
                "capabilities_etag",
                "",
            ),
            (
                DastReadinessCode.CATALOG_STALE,
                binding.target.integration.dast_state,
                "capabilities_synced_at",
                self.now - timedelta(hours=25),
            ),
            (
                DastReadinessCode.CATALOG_SYNC_FAILED,
                binding.target.integration.dast_state,
                "sync_error_code",
                "TIMEOUT",
            ),
            (
                DastReadinessCode.BINDING_DISABLED,
                binding,
                "enabled",
                False,
            ),
            (
                DastReadinessCode.TARGET_UNAVAILABLE,
                binding.target,
                "is_available",
                False,
            ),
            (
                DastReadinessCode.TARGET_CATALOG_INVALID,
                binding.target,
                "provider_defaults",
                {"unknown": True},
            ),
            (
                DastReadinessCode.TARGET_CONTRACT_INCOMPATIBLE,
                binding.target,
                "contract_revision",
                "3.0",
            ),
            (
                DastReadinessCode.BINDING_PARAMETERS_INVALID,
                binding,
                "parameter_snapshot",
                {"unknown": True},
            ),
            (
                DastReadinessCode.SOURCE_REPOSITORY_UNAVAILABLE,
                binding,
                "source_repo_key",
                "removed",
            ),
            (
                DastReadinessCode.AUTONOMOUS_POLICY_DISABLED,
                binding,
                "autonomous_enabled",
                False,
            ),
            (
                DastReadinessCode.AUTONOMOUS_TARGET_NOT_READY,
                binding.target,
                "autonomous_ready",
                False,
            ),
        )
        for code, subject, field, value in cases:
            with self.subTest(code=code):
                original = getattr(subject, field)
                setattr(subject, field, value)
                try:
                    result = check_dast_binding_readiness(binding, now=self.now)
                    self.assertIn(code, {issue.code for issue in result.issues})
                finally:
                    setattr(subject, field, original)

    def test_vpn_and_private_gateway_prerequisites_are_table_driven(self):
        binding = self._fresh_binding()
        integration = binding.target.integration
        integration.config = _integration_config("https://10.23.4.5")
        initial = check_dast_binding_readiness(binding, now=self.now)
        self.assertIn(DastReadinessCode.PRIVATE_GATEWAY_REQUIRES_VPN, {issue.code for issue in initial.issues})

        vpn = OrgIntegration.objects.create(
            organization=self.organization,
            integration_type=OrgIntegrationType.VPN,
            name="Readiness VPN",
            is_active=True,
        )
        vpn_secret = OrgIntegrationVPNSecret.objects.create(integration=vpn, ovpn_content="client\ndev tun")
        integration.vpn_integration = vpn
        self.assertTrue(check_dast_binding_readiness(binding, now=self.now).ready)

        other_product_type = Product_Type.objects.create(name="Other readiness PT")
        other_organization = Organization.objects.create(name="Other readiness org", product_type=other_product_type)
        cases = (
            (
                DastReadinessCode.VPN_TYPE_INVALID,
                vpn,
                "integration_type",
                OrgIntegrationType.GITLAB,
            ),
            (
                DastReadinessCode.VPN_ORGANIZATION_MISMATCH,
                vpn,
                "organization_id",
                other_organization.pk,
            ),
            (
                DastReadinessCode.VPN_INACTIVE,
                vpn,
                "is_active",
                False,
            ),
            (
                DastReadinessCode.VPN_CREDENTIALS_MISSING,
                vpn_secret,
                "ovpn_content",
                "",
            ),
            (
                DastReadinessCode.VPN_USER_PASSWORD_INCOMPLETE,
                vpn_secret,
                "vpn_username",
                "vpn-user",
            ),
        )
        for code, subject, field, value in cases:
            with self.subTest(code=code):
                original = getattr(subject, field)
                setattr(subject, field, value)
                try:
                    result = check_dast_binding_readiness(binding, now=self.now)
                    codes = {issue.code for issue in result.issues}
                    self.assertIn(code, codes)
                    self.assertIn(DastReadinessCode.PRIVATE_GATEWAY_REQUIRES_VPN, codes)
                finally:
                    setattr(subject, field, original)
