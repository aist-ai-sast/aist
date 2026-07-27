from django.urls import reverse

from aist.models import (
    AISTProjectLaunchConfig,
    LaunchSchedule,
    Organization,
    PipelineExecutionType,
    PipelineLaunchRequest,
    PipelineLaunchRequestState,
)
from aist.pipeline_args import PipelineArguments
from aist.test.test_api import AISTApiBase


class LegacySastExecutionCompatibilityTests(AISTApiBase):
    def setUp(self):
        super().setUp()
        self.organization = Organization.objects.create(
            name="Legacy SAST execution organization",
            product_type=self.prod_type,
        )
        self.config = AISTProjectLaunchConfig.objects.create(
            project=self.project,
            name="Existing SAST preset",
            params={"project_version": {"id": self.pv.id}, "analyzers": ["semgrep"]},
            is_default=True,
        )
        self.schedule = LaunchSchedule.objects.create(
            cron_expression="*/5 * * * *",
            launch_config=self.config,
        )

    def test_existing_config_list_preserves_sast_values(self):
        response = self.client.get(
            reverse("aist_api:project_launch_config_list_create", kwargs={"project_id": self.project.id}),
        )

        self.assertEqual(response.status_code, 200)
        payload = response.data[0]
        self.assertEqual(payload["name"], "Existing SAST preset")
        self.assertEqual(payload["params"], self.config.params)
        self.assertTrue(payload["is_default"])
        self.assertEqual(payload["execution_type"], PipelineExecutionType.SAST)
        self.assertIsNone(payload["dast_binding"])

    def test_existing_run_once_and_queue_shapes_remain_available(self):
        response = self.client.post(
            reverse(
                "aist_api:launch_schedule_run_once",
                kwargs={"launch_schedule_id": self.schedule.id},
            ),
            data={},
            format="json",
        )
        self.assertEqual(response.status_code, 200)
        legacy_item = response.data["queue_item"]
        self.assertFalse(legacy_item["dispatched"])
        self.assertIsNone(legacy_item["dispatched_at"])
        self.assertEqual(legacy_item["launch_config_id"], self.config.id)

        request = PipelineLaunchRequest.objects.get(pk=legacy_item["id"])
        self.assertEqual(request.execution_type, PipelineExecutionType.SAST)
        self.assertEqual(request.state, PipelineLaunchRequestState.PENDING)
        self.assertEqual(
            request.params_snapshot,
            PipelineArguments.normalize_params(project=self.project, raw_params=self.config.params),
        )

        queue_response = self.client.get(
            reverse("aist_api:pipeline_launch_request_list"),
            data={"only_pending": True},
        )
        self.assertEqual(queue_response.status_code, 200)
        self.assertEqual(queue_response.data["results"][0]["id"], request.id)
        self.assertFalse(queue_response.data["results"][0]["dispatched"])
