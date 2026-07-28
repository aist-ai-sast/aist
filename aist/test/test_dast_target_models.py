from copy import deepcopy

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import TestCase
from django.utils import timezone
from dojo.models import Product, Product_Type, SLA_Configuration

from aist.integrations.dast_config import DastBindingParameters, DastConfigError, DastTargetSnapshot
from aist.models import (
    AISTProject,
    DastProjectBinding,
    DastTarget,
    Organization,
    OrgIntegration,
    OrgIntegrationType,
)
from aist.services.dast_targets import refresh_dast_targets


def _parameter_schema():
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "additionalProperties": False,
        "properties": {"depth": {"enum": ["light", "deep"]}},
        "required": ["depth"],
    }


def _target_wire(provider_id="app", **overrides):
    payload = {
        "id": provider_id,
        "display_name": f"{provider_id} API",
        "contract_revision": "2.0",
        "capability_revision": f"sha256:{provider_id}-capability",
        "schema_digest": f"sha256:{provider_id}-schema",
        "parameter_schema": _parameter_schema(),
        "defaults": {"depth": "light"},
        "repository_keys": [provider_id, f"{provider_id}-frontend"],
        "autonomous_ready": True,
    }
    payload.update(overrides)
    return payload


def _integration_config(public_id):
    return {
        "gateway_url": "https://dast-gateway.internal",
        "ca_bundle": "",
        "contract_major": 2,
        "integrator_public_id": public_id,
        "server_fingerprint": "sha256:server-fingerprint",
    }


class DastTargetSnapshotTests(TestCase):
    def test_target_and_binding_snapshots_are_strict_schema_valid_defensive_copies(self):
        wire = _target_wire()
        snapshot = DastTargetSnapshot.from_snapshot(wire)
        wire["parameter_schema"]["properties"].clear()

        parameters = DastBindingParameters.from_snapshot({"depth": "deep"}, target=snapshot)
        serialized = parameters.to_snapshot()
        serialized["depth"] = "light"
        self.assertEqual(parameters.values, {"depth": "deep"})
        self.assertIn("depth", snapshot.parameter_schema["properties"])

        with self.assertRaises(DastConfigError):
            DastBindingParameters.from_snapshot({"depth": "unsupported"}, target=snapshot)
        with self.assertRaises(DastConfigError):
            DastTargetSnapshot.from_snapshot({**_target_wire(), "legacy_defaults": {}})

    def test_oversized_target_strings_are_refused_before_they_reach_storage(self):
        """
        A gateway once returned a 259-character label for a 255-character column, and the resulting
        database error aborted the whole atomic refresh -- every target was lost over one field, and
        the recorded outcome named neither the target nor the field. Refuse it here instead.
        """
        oversized = {
            "id": DastTargetSnapshot.MAX_PROVIDER_ID_LENGTH,
            "display_name": DastTargetSnapshot.MAX_DISPLAY_NAME_LENGTH,
            "contract_revision": DastTargetSnapshot.MAX_CONTRACT_REVISION_LENGTH,
            "capability_revision": DastTargetSnapshot.MAX_CAPABILITY_REVISION_LENGTH,
            "schema_digest": DastTargetSnapshot.MAX_SCHEMA_DIGEST_LENGTH,
        }
        for field, limit in oversized.items():
            with self.subTest(field=field):
                # At the limit the value is still accepted; one character past it is not.
                DastTargetSnapshot.from_snapshot(_target_wire(**{field: "x" * limit}))
                with self.assertRaises(DastConfigError) as caught:
                    DastTargetSnapshot.from_snapshot(_target_wire(**{field: "x" * (limit + 1)}))
                self.assertIn(field, str(caught.exception))

    def test_snapshot_limits_stay_in_step_with_the_columns_they_protect(self):
        # The limits exist to keep storage from being what rejects a catalog, so a widened column
        # with a stale limit would silently reintroduce the refusal it was added to prevent.
        for field_name, limit in (
            ("provider_id", DastTargetSnapshot.MAX_PROVIDER_ID_LENGTH),
            ("display_name", DastTargetSnapshot.MAX_DISPLAY_NAME_LENGTH),
            ("contract_revision", DastTargetSnapshot.MAX_CONTRACT_REVISION_LENGTH),
            ("capability_revision", DastTargetSnapshot.MAX_CAPABILITY_REVISION_LENGTH),
            ("schema_digest", DastTargetSnapshot.MAX_SCHEMA_DIGEST_LENGTH),
        ):
            with self.subTest(field=field_name):
                self.assertEqual(DastTarget._meta.get_field(field_name).max_length, limit)


class DastTargetAndBindingModelTests(TestCase):
    def setUp(self):
        self.sla = SLA_Configuration.objects.create(name="DAST target SLA")
        self.organization, self.project = self._organization_and_project("Primary")
        self.other_organization, self.other_project = self._organization_and_project("Other")
        self.integration = self._integration(self.organization, "Primary", is_active=True)
        self.other_integration = self._integration(self.other_organization, "Other", is_active=True)

    def _organization_and_project(self, prefix):
        product_type = Product_Type.objects.create(name=f"{prefix} DAST target PT")
        organization = Organization.objects.create(
            name=f"{prefix} DAST target org",
            product_type=product_type,
        )
        product = Product.objects.create(
            name=f"{prefix} DAST target product",
            description="",
            prod_type=product_type,
            sla_configuration=self.sla,
        )
        return organization, AISTProject.objects.create(product=product)

    @staticmethod
    def _integration(organization, name, *, is_active):
        return OrgIntegration.objects.create(
            organization=organization,
            integration_type=OrgIntegrationType.DAST,
            name=name,
            config=_integration_config(f"pub_{name.lower()}"),
            is_active=is_active,
        )

    def _refresh(self, integration, *payloads):
        snapshots = [DastTargetSnapshot.from_snapshot(payload) for payload in payloads]
        return refresh_dast_targets(integration, snapshots, seen_at=timezone.now())

    def test_refresh_updates_display_and_marks_removed_targets_unavailable_without_deleting(self):
        app, frontend = self._refresh(self.integration, _target_wire("app"), _target_wire("frontend"))
        app_pk = app.pk
        frontend_pk = frontend.pk

        refreshed = self._refresh(
            self.integration,
            _target_wire("app", display_name="Renamed application API"),
        )

        self.assertEqual(refreshed[0].pk, app_pk)
        self.assertEqual(refreshed[0].display_name, "Renamed application API")
        self.assertFalse(DastTarget.objects.get(pk=frontend_pk).is_available)
        self.assertEqual(DastTarget.objects.filter(integration=self.integration).count(), 2)

    def test_target_identity_is_unique_per_integration_and_cannot_be_reparented(self):
        target = self._refresh(self.integration, _target_wire())[0]
        duplicate_values = {
            field: deepcopy(getattr(target, field))
            for field in (
                "display_name",
                "contract_revision",
                "capability_revision",
                "schema_digest",
                "parameter_schema",
                "provider_defaults",
                "repository_keys",
                "autonomous_ready",
                "is_available",
                "last_seen_at",
            )
        }
        with self.assertRaises(IntegrityError), transaction.atomic():
            DastTarget.objects.create(
                integration=self.integration,
                provider_id=target.provider_id,
                **duplicate_values,
            )

        target.integration = self.other_integration
        with self.assertRaises(ValidationError):
            target.full_clean()

    def test_project_supports_multiple_targets_but_not_duplicate_binding(self):
        app, frontend = self._refresh(self.integration, _target_wire("app"), _target_wire("frontend"))
        first = DastProjectBinding(
            project=self.project,
            target=app,
            source_repo_key="app",
            parameter_snapshot={"depth": "light"},
            autonomous_enabled=True,
        )
        second = DastProjectBinding(
            project=self.project,
            target=frontend,
            source_repo_key="frontend",
            parameter_snapshot={"depth": "deep"},
        )
        first.full_clean()
        first.save()
        second.full_clean()
        second.save()
        self.assertEqual(self.project.dast_bindings.count(), 2)

        with self.assertRaises(IntegrityError), transaction.atomic():
            DastProjectBinding.objects.create(
                project=self.project,
                target=app,
                source_repo_key="app",
                parameter_snapshot={"depth": "light"},
            )

    def test_binding_rejects_cross_org_stale_integration_repository_and_parameter_mismatches(self):
        own_target = self._refresh(self.integration, _target_wire("app"))[0]
        other_target = self._refresh(self.other_integration, _target_wire("other"))[0]

        invalid_bindings = (
            DastProjectBinding(
                project=self.project,
                target=other_target,
                source_repo_key="other",
                parameter_snapshot={"depth": "light"},
            ),
            DastProjectBinding(
                project=self.project,
                target=own_target,
                source_repo_key="not-advertised",
                parameter_snapshot={"depth": "light"},
            ),
            DastProjectBinding(
                project=self.project,
                target=own_target,
                source_repo_key="app",
                parameter_snapshot={"depth": "unsupported"},
            ),
        )
        for binding in invalid_bindings:
            with self.subTest(binding=binding), self.assertRaises(ValidationError):
                binding.full_clean()

        with self.assertRaises(IntegrityError), transaction.atomic():
            DastProjectBinding.objects.create(
                project=self.project,
                target=other_target,
                source_repo_key="other",
                parameter_snapshot={"depth": "light"},
            )
        with self.assertRaises(IntegrityError), transaction.atomic():
            DastProjectBinding.objects.create(
                project=self.project,
                target=own_target,
                source_repo_key="not-advertised",
                parameter_snapshot={"depth": "light"},
            )

        self.integration.is_active = False
        self.integration.save(update_fields=["is_active"])
        stale_binding = DastProjectBinding(
            project=self.project,
            target=own_target,
            source_repo_key="app",
            parameter_snapshot={"depth": "light"},
        )
        with self.assertRaises(ValidationError):
            stale_binding.full_clean()
