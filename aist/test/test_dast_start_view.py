from __future__ import annotations

import json

from django.test import Client
from django.urls import reverse
from django.utils import timezone

from aist.integrations.dast_config import DastTargetSnapshot
from aist.models import (
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
