from unittest.mock import patch

from django.urls import reverse
from django.utils import timezone
from dojo.authorization.roles_permissions import Roles
from dojo.models import Product_Type, Product_Type_Member, Role

from aist.integrations.dast_config import DastTargetSnapshot
from aist.models import (
    AISTProjectLaunchConfig,
    DastIntegrationState,
    DastIntegrationValidationState,
    DastProjectBinding,
    Organization,
    OrgIntegration,
    OrgIntegrationType,
    PipelineExecutionType,
    PipelineLaunchRequest,
    PipelineLaunchRequestState,
)
from aist.services.dast_targets import refresh_dast_targets
from aist.test.test_api import AISTApiBase


def _config(public_id):
    return {
        "gateway_url": "https://gateway.example",
        "ca_bundle": "",
        "contract_major": 2,
        "integrator_public_id": public_id,
        "server_fingerprint": "sha256:server-fingerprint",
    }


def _target(provider_id="app"):
    return DastTargetSnapshot.from_snapshot({
        "id": provider_id,
        "display_name": f"{provider_id} API",
        "contract_revision": "2.0",
        "capability_revision": f"sha256:{provider_id}-capability",
        "schema_digest": f"sha256:{provider_id}-schema",
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


class DastTargetBindingAPITests(AISTApiBase):

    def setUp(self):
        super().setUp()
        self.organization = Organization.objects.create(name="DAST binding API org", product_type=self.prod_type)
        self.project.refresh_from_db()
        self.integration = OrgIntegration.objects.create(
            organization=self.organization,
            integration_type=OrgIntegrationType.DAST,
            name="DAST",
            config=_config("pub_aist"),
            is_active=True,
        )
        DastIntegrationState.objects.create(
            integration=self.integration,
            validation_state=DastIntegrationValidationState.READY,
            contract_version="2.0",
        )
        self.target = refresh_dast_targets(self.integration, (_target(),))[0]
        self.catalog_url = reverse(
            "aist_api:organization_dast_target_catalog",
            kwargs={"org_id": self.organization.pk},
        )
        self.bindings_url = reverse(
            "aist_api:project_dast_binding_list_create",
            kwargs={"project_id": self.project.pk},
        )

    def _binding_payload(self, **overrides):
        payload = {
            "target_id": self.target.pk,
            "capability_revision": self.target.capability_revision,
            "schema_digest": self.target.schema_digest,
            "source_repo_key": "source",
            "enabled": True,
            "parameter_snapshot": {},
            "autonomous_enabled": True,
        }
        payload.update(overrides)
        return payload

    def _launch_configs_url(self):
        return reverse(
            "aist_api:project_launch_config_list_create",
            kwargs={"project_id": self.project.pk},
        )

    def test_catalog_is_safe_and_binding_upsert_is_schema_and_revision_checked(self):
        catalog = self.client.get(self.catalog_url)
        self.assertEqual(catalog.status_code, 200)
        self.assertEqual(catalog.data[0]["provider_id"], "app")
        self.assertNotIn("integration", catalog.data[0])
        self.assertNotIn("config", catalog.data[0])

        created = self.client.post(self.bindings_url, self._binding_payload(), format="json")
        self.assertEqual(created.status_code, 200, created.data)
        self.assertFalse(created.data["readiness"]["ready"])
        self.assertIn(
            "CATALOG_NOT_SYNCED",
            {issue["code"] for issue in created.data["readiness"]["issues"]},
        )
        self.assertEqual(DastProjectBinding.objects.filter(project=self.project).count(), 1)
        updated = self.client.post(
            self.bindings_url,
            self._binding_payload(autonomous_enabled=False),
            format="json",
        )
        self.assertEqual(updated.status_code, 200, updated.data)
        self.assertFalse(DastProjectBinding.objects.get(project=self.project).autonomous_enabled)

        for field in ("capability_revision", "schema_digest"):
            with self.subTest(field=field):
                response = self.client.post(
                    self.bindings_url,
                    self._binding_payload(**{field: "stale"}),
                    format="json",
                )
                self.assertEqual(response.status_code, 400)

    def test_cross_org_target_and_unadvertised_repository_are_rejected(self):
        other_product_type = Product_Type.objects.create(name="Other DAST binding PT")
        other_organization = Organization.objects.create(name="Other DAST binding org", product_type=other_product_type)
        other_integration = OrgIntegration.objects.create(
            organization=other_organization,
            integration_type=OrgIntegrationType.DAST,
            name="Other DAST",
            config=_config("pub_other"),
        )
        other_target = refresh_dast_targets(other_integration, (_target("other"),))[0]

        cross_org = self.client.post(
            self.bindings_url,
            self._binding_payload(
                target_id=other_target.pk,
                capability_revision=other_target.capability_revision,
                schema_digest=other_target.schema_digest,
            ),
            format="json",
        )
        bad_repository = self.client.post(
            self.bindings_url,
            self._binding_payload(source_repo_key="not-advertised"),
            format="json",
        )
        invalid_target_pk = self.client.post(
            self.bindings_url,
            self._binding_payload(target_id="not-a-pk"),
            format="json",
        )
        self.assertEqual(cross_org.status_code, 400)
        self.assertEqual(bad_repository.status_code, 400)
        self.assertEqual(invalid_target_pk.status_code, 400)
        self.assertIn("target_id", invalid_target_pk.data)

    def test_binding_rejects_mass_assignment_of_server_owned_fields(self):
        payload = {
            **self._binding_payload(),
            "project": self.other_project.pk,
            "target": {"provider_id": "attacker-controlled"},
            "readiness": {"ready": True},
        }

        response = self.client.post(self.bindings_url, payload, format="json")

        self.assertEqual(response.status_code, 400)
        self.assertFalse(DastProjectBinding.objects.filter(project=self.project).exists())

    def test_ready_binding_can_be_saved_as_a_dast_launch_config_through_the_public_api(self):
        binding_response = self.client.post(self.bindings_url, self._binding_payload(), format="json")
        self.assertEqual(binding_response.status_code, 200, binding_response.data)

        response = self.client.post(
            self._launch_configs_url(),
            {
                "name": "Web application DAST",
                "description": "Autonomous staging scan",
                "execution_type": PipelineExecutionType.DAST,
                "dast_binding_id": binding_response.data["id"],
                "trigger_project_version_id": self.pv.pk,
                "params": {},
                "is_default": False,
            },
            format="json",
        )

        self.assertEqual(response.status_code, 201, response.data)
        config = AISTProjectLaunchConfig.objects.get(pk=response.data["id"])
        self.assertEqual(config.project, self.project)
        self.assertEqual(config.execution_type, PipelineExecutionType.DAST)
        self.assertEqual(config.dast_binding_id, binding_response.data["id"])
        self.assertEqual(config.params, {})
        self.assertEqual(response.data["dast_target_label"], "app API")
        self.assertEqual(response.data["dast_source_repository"], "source")
        self.assertEqual(response.data["trigger_project_version_id"], self.pv.pk)

        listed = self.client.get(self._launch_configs_url())
        self.assertEqual(listed.status_code, 200, listed.data)
        self.assertEqual(listed.data[0]["dast_target_label"], "app API")
        self.assertEqual(listed.data[0]["dast_source_repository"], "source")

    def test_dast_launch_config_update_selects_a_scoped_enabled_binding(self):
        targets = refresh_dast_targets(self.integration, (_target(), _target("mobile")))
        self.target = targets[0]
        first_response = self.client.post(self.bindings_url, self._binding_payload(), format="json")
        self.assertEqual(first_response.status_code, 200, first_response.data)
        mobile = targets[1]
        second_response = self.client.post(
            self.bindings_url,
            self._binding_payload(
                target_id=mobile.pk,
                capability_revision=mobile.capability_revision,
                schema_digest=mobile.schema_digest,
            ),
            format="json",
        )
        self.assertEqual(second_response.status_code, 200, second_response.data)
        created = self.client.post(
            self._launch_configs_url(),
            {
                "name": "Switchable DAST",
                "execution_type": PipelineExecutionType.DAST,
                "dast_binding_id": first_response.data["id"],
                "trigger_project_version_id": self.pv.pk,
                "params": {},
            },
            format="json",
        )
        detail_url = reverse(
            "aist_api:project_launch_config_detail",
            kwargs={"project_id": self.project.pk, "config_id": created.data["id"]},
        )

        updated = self.client.patch(
            detail_url,
            {"dast_binding_id": second_response.data["id"]},
            format="json",
        )

        self.assertEqual(updated.status_code, 200, updated.data)
        self.assertEqual(updated.data["dast_binding"], second_response.data["id"])
        self.assertEqual(updated.data["dast_target_label"], "mobile API")

        second_binding = DastProjectBinding.objects.get(pk=second_response.data["id"])
        second_binding.enabled = False
        second_binding.save(update_fields=["enabled"])
        rejected = self.client.patch(
            detail_url,
            {"dast_binding_id": second_binding.pk},
            format="json",
        )
        self.assertEqual(rejected.status_code, 400, rejected.data)
        self.assertIn("dast_binding_id", rejected.data)

    def test_dast_launch_config_create_rejects_missing_binding_analyzers_and_server_fields(self):
        binding_response = self.client.post(self.bindings_url, self._binding_payload(), format="json")
        self.assertEqual(binding_response.status_code, 200, binding_response.data)
        base = {
            "name": "Strict DAST config",
            "execution_type": PipelineExecutionType.DAST,
            "dast_binding_id": binding_response.data["id"],
            "trigger_project_version_id": self.pv.pk,
            "params": {},
        }

        cases = (
            ({key: value for key, value in base.items() if key != "dast_binding_id"}, "dast_binding_id"),
            ({**base, "params": {"analyzers": ["semgrep"]}}, "params"),
            ({**base, "dast_binding_id": "not-a-pk"}, "dast_binding_id"),
            ({**base, "trigger_project_version_id": "not-a-pk"}, "trigger_project_version_id"),
            ({**base, "project": self.project.pk}, "project"),
            ({**base, "dast_binding": binding_response.data["id"]}, "dast_binding"),
        )
        for payload, error_field in cases:
            with self.subTest(error_field=error_field):
                response = self.client.post(self._launch_configs_url(), payload, format="json")
                self.assertEqual(response.status_code, 400, response.data)
                self.assertIn(error_field, response.data)

        self.assertFalse(AISTProjectLaunchConfig.objects.exists())

    def test_dast_launch_config_rejects_a_binding_from_another_organization(self):
        other_organization = Organization.objects.create(
            name="Other launch config org",
            product_type=self.other_prod_type,
        )
        other_integration = OrgIntegration.objects.create(
            organization=other_organization,
            integration_type=OrgIntegrationType.DAST,
            name="Other launch config DAST",
            config=_config("pub_other_launch"),
            is_active=True,
        )
        DastIntegrationState.objects.create(
            integration=other_integration,
            validation_state=DastIntegrationValidationState.READY,
            contract_version="2.0",
        )
        other_target = refresh_dast_targets(other_integration, (_target("other-launch"),))[0]
        other_binding = DastProjectBinding.objects.create(
            project=self.other_project,
            target=other_target,
            source_repo_key="source",
            enabled=True,
            parameter_snapshot={},
            autonomous_enabled=True,
        )

        response = self.client.post(
            self._launch_configs_url(),
            {
                "name": "Cross-organization DAST",
                "execution_type": PipelineExecutionType.DAST,
                "dast_binding_id": other_binding.pk,
                "trigger_project_version_id": self.pv.pk,
                "params": {},
            },
            format="json",
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("dast_binding_id", response.data)
        self.assertFalse(AISTProjectLaunchConfig.objects.exists())

    def test_dast_launch_returns_backend_readiness_reasons_before_dispatch(self):
        binding_response = self.client.post(self.bindings_url, self._binding_payload(), format="json")
        self.assertEqual(binding_response.status_code, 200, binding_response.data)
        binding = DastProjectBinding.objects.get(project=self.project)
        config = AISTProjectLaunchConfig.objects.create(
            project=self.project,
            name="DAST readiness launch",
            execution_type=PipelineExecutionType.DAST,
            dast_binding=binding,
            trigger_project_version=self.pv,
            params={},
        )
        start_url = reverse(
            "aist_api:project_launch_config_start",
            kwargs={"project_id": self.project.pk, "config_id": config.pk},
        )

        response = self.client.post(start_url, {}, format="json")

        self.assertEqual(response.status_code, 409)
        self.assertIn("readiness", response.data)
        self.assertNotIn("execution_type", response.data)
        codes = {issue["code"] for issue in response.data["readiness"]["issues"]}
        self.assertIn("INTEGRATION_TOKEN_MISSING", codes)
        self.assertIn("CATALOG_NOT_SYNCED", codes)

        self.integration.secret = "runtime-token"  # noqa: S105 -- test fixture
        self.integration.save(update_fields=["secret", "updated"])
        state = self.integration.dast_state
        state.capabilities_etag = "catalog-1"
        state.capabilities_synced_at = timezone.now()
        state.save(update_fields=["capabilities_etag", "capabilities_synced_at", "updated"])
        ready_response = self.client.post(start_url, {}, format="json")
        self.assertEqual(ready_response.status_code, 202, ready_response.data)
        launch_request = PipelineLaunchRequest.objects.get(pk=ready_response.data["id"])
        self.assertEqual(launch_request.state, PipelineLaunchRequestState.PENDING)
        self.assertEqual(launch_request.execution_type, PipelineExecutionType.DAST)
        self.assertEqual(launch_request.trigger_project_version_id, self.pv.pk)
        self.assertNotIn("project_version", launch_request.params_snapshot)

    def test_reader_can_list_but_writer_cannot_mutate_binding(self):
        reader, _created = Role.objects.get_or_create(id=Roles.Reader, defaults={"name": "Reader"})
        membership = Product_Type_Member.objects.get(product_type=self.prod_type, user=self.user)
        membership.role = reader
        membership.save(update_fields=["role"])
        self.assertEqual(self.client.get(self.bindings_url).status_code, 200)

        writer, _created = Role.objects.get_or_create(id=Roles.Writer, defaults={"name": "Writer"})
        membership.role = writer
        membership.save(update_fields=["role"])
        self.assertEqual(self.client.post(self.bindings_url, self._binding_payload(), format="json").status_code, 404)
        self.assertEqual(
            self.client.post(
                self._launch_configs_url(),
                {
                    "name": "Forbidden DAST config",
                    "execution_type": PipelineExecutionType.DAST,
                    "dast_binding_id": 1,
                    "params": {},
                },
                format="json",
            ).status_code,
            404,
        )

    @patch("aist.integrations.dast_capability_sync.current_app.send_task")
    def test_manual_capability_sync_is_queued_only_for_ready_integration(self, mock_send_task):
        sync_url = reverse(
            "aist_api:dast_integration_sync_capabilities",
            kwargs={"integration_id": self.integration.pk},
        )
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(sync_url, format="json")

        self.assertEqual(response.status_code, 202)
        mock_send_task.assert_called_once()
        self.assertEqual(mock_send_task.call_args.args[0], "aist.tasks.validate.sync_dast_capabilities")
        self.integration.dast_state.validation_state = DastIntegrationValidationState.INVALID
        self.integration.dast_state.save(update_fields=["validation_state"])
        self.assertEqual(self.client.post(sync_url, format="json").status_code, 409)

    def test_binding_can_be_deleted_through_authorized_binding_root(self):
        created = self.client.post(self.bindings_url, self._binding_payload(), format="json")
        self.assertEqual(created.status_code, 200, created.data)
        binding = DastProjectBinding.objects.get(project=self.project)
        detail_url = reverse(
            "aist_api:project_dast_binding_detail",
            kwargs={"binding_id": binding.pk},
        )

        response = self.client.delete(detail_url)

        self.assertEqual(response.status_code, 204)
        self.assertFalse(DastProjectBinding.objects.filter(pk=binding.pk).exists())
