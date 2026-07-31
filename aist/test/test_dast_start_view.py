from __future__ import annotations

import json

from django.test import Client
from django.urls import reverse
from django.utils import timezone
from dojo.models import Product

from aist.integrations.dast_config import DastTargetSnapshot
from aist.models import (
    AISTProject,
    AISTProjectLaunchConfig,
    AISTProjectVersion,
    DastIntegrationState,
    DastIntegrationValidationState,
    DastProjectBinding,
    Organization,
    OrgIntegration,
    OrgIntegrationType,
    PipelineExecutionType,
    PipelineLaunchRequest,
    VersionType,
)
from aist.services.dast_targets import refresh_dast_targets
from aist.test.test_api import AISTApiBase
from aist.test.test_dast_target_models import _integration_config, _target_wire


class DastStartViewTests(AISTApiBase):
    def setUp(self):
        super().setUp()
        self.user.is_staff = True
        self.user.is_superuser = True
        self.user.save(update_fields=["is_staff", "is_superuser"])
        self.client = Client()
        self.client.force_login(self.user)
        self.organization = Organization.objects.create(
            name="DAST start organization",
            product_type=self.prod_type,
        )
        self.integration = OrgIntegration.objects.create(
            organization=self.organization,
            integration_type=OrgIntegrationType.DAST,
            name="DAST start gateway",
            config=_integration_config("dast-start"),
            secret="runtime-token",  # noqa: S106 -- test fixture
            is_active=True,
        )
        now = timezone.now()
        DastIntegrationState.objects.create(
            integration=self.integration,
            validation_state=DastIntegrationValidationState.READY,
            validated_at=now,
            contract_version="2.0",
            capabilities_etag="start-catalog",
            capabilities_synced_at=now,
        )
        self.target = refresh_dast_targets(
            self.integration,
            [DastTargetSnapshot.from_snapshot(_target_wire("start-api"))],
            seen_at=now,
        )[0]
        self.binding = DastProjectBinding.objects.create(
            project=self.project,
            target=self.target,
            source_repo_key="start-api",
            enabled=True,
            autonomous_enabled=True,
            parameter_snapshot={"depth": "light"},
        )

    def _payload(self, **overrides):
        payload = {
            "execution_type": "DAST",
            "project": self.project.pk,
            "dast_binding": self.binding.pk,
            "trigger_project_version": self.pv.pk,
            "parameters": json.dumps({"depth": "deep"}),
            "client_request_key": "one-off-dast-1",
            "one_off_actions": "[]",
        }
        payload.update(overrides)
        return payload

    def test_one_off_dast_launch_creates_only_a_durable_request(self):
        response = self.client.post(reverse("aist:start_pipeline"), self._payload())

        self.assertEqual(response.status_code, 302)
        request = PipelineLaunchRequest.objects.get()
        self.assertEqual(request.execution_type, PipelineExecutionType.DAST)
        self.assertEqual(request.dast_binding, self.binding)
        self.assertEqual(request.trigger_project_version, self.pv)
        self.assertEqual(request.params_snapshot, {"depth": "deep"})
        self.assertEqual(AISTProjectLaunchConfig.objects.count(), 0)

    def test_one_off_dast_launch_is_idempotent(self):
        first = self.client.post(reverse("aist:start_pipeline"), self._payload())
        second = self.client.post(reverse("aist:start_pipeline"), self._payload())

        self.assertEqual(first.status_code, 302)
        self.assertEqual(second.status_code, 302)
        self.assertEqual(PipelineLaunchRequest.objects.count(), 1)

    def test_dast_form_options_are_human_readable_and_project_scoped(self):
        response = self.client.get(reverse("aist:start_pipeline"), {"execution_type": "DAST"})

        self.assertEqual(response.status_code, 200)
        content = response.content.decode()
        # Human-readable label (target display name + repo key), not the model's
        # default __str__ ("DAST binding[<project_id>:<target_id>]") that gave the
        # operator no way to tell bindings apart across projects.
        self.assertIn("start-api API", content)
        self.assertNotIn(f"DAST binding[{self.project.pk}:{self.target.pk}]", content)
        # Each option carries a data-project attribute so the page's cascading JS
        # can filter the binding/version lists down to the selected project.
        self.assertIn(f'data-project="{self.project.pk}"', content)

    def test_dast_launch_rejects_binding_from_a_different_project(self):
        second_product = Product.objects.create(
            name="Second product, same team",
            description="desc",
            prod_type=self.prod_type,
            sla_configuration_id=self.sla.id,
        )
        other_binding_project = AISTProject.objects.create(
            product=second_product,
            supported_languages=["python"],
            compilable=False,
            profile={},
        )
        other_binding = DastProjectBinding.objects.create(
            project=other_binding_project,
            target=self.target,
            source_repo_key="start-api",
            enabled=True,
            autonomous_enabled=True,
            parameter_snapshot={"depth": "light"},
        )

        response = self.client.post(
            reverse("aist:start_pipeline"),
            self._payload(dast_binding=other_binding.pk),
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "must belong to the selected project")
        self.assertFalse(PipelineLaunchRequest.objects.exists())

    def test_dast_launch_rejects_file_source_disabled_binding_and_actions(self):
        file_version = AISTProjectVersion.objects.create(
            project=self.project,
            version_type=VersionType.FILE_HASH,
            version="archive",
        )
        response = self.client.post(
            reverse("aist:start_pipeline"),
            self._payload(trigger_project_version=file_version.pk),
        )
        self.assertEqual(response.status_code, 200)

        self.binding.enabled = False
        self.binding.save(update_fields=["enabled", "updated"])
        response = self.client.post(reverse("aist:start_pipeline"), self._payload())
        self.assertEqual(response.status_code, 200)

        self.binding.enabled = True
        self.binding.save(update_fields=["enabled", "updated"])
        response = self.client.post(
            reverse("aist:start_pipeline"),
            self._payload(one_off_actions=json.dumps([{"action_type": "WRITE_LOG"}])),
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "available only for SAST")
        self.assertFalse(PipelineLaunchRequest.objects.exists())
