from unittest.mock import patch

from django.urls import reverse
from django.utils import timezone
from dojo.authorization.roles_permissions import Roles
from dojo.models import Product_Type_Member, Role
from rest_framework.test import APIClient

from aist.integrations.dast_config import DastTargetSnapshot
from aist.models import (
    AISTApiToken,
    AISTPipeline,
    AISTStatus,
    ApiTokenScope,
    DastExecutionOutcome,
    DastExecutionState,
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
from aist.tasks import pipeline as pipeline_tasks
from aist.test.test_api import AISTApiBase
from aist.test.test_dast_target_models import _integration_config, _target_wire
from aist.utils.pipeline import stop_pipeline


class DastCancellationTests(AISTApiBase):
    def setUp(self):
        super().setUp()
        self.organization = Organization.objects.create(
            name="DAST cancellation fixture organization",
            product_type=self.prod_type,
        )
        integration = OrgIntegration.objects.create(
            organization=self.organization,
            integration_type=OrgIntegrationType.DAST,
            name="DAST cancellation fixture",
            config=_integration_config("dast-cancellation-public-id"),
            secret="runtime-token",  # noqa: S106 -- test fixture
            is_active=True,
        )
        now = timezone.now()
        DastIntegrationState.objects.create(
            integration=integration,
            validation_state=DastIntegrationValidationState.READY,
            validated_at=now,
            contract_version="2.0",
            capabilities_etag="cancellation-catalog",
            capabilities_synced_at=now,
        )
        target = refresh_dast_targets(
            integration,
            (DastTargetSnapshot.from_snapshot(_target_wire("cancellation-api")),),
            seen_at=now,
        )[0]
        self.binding = DastProjectBinding.objects.create(
            project=self.project,
            target=target,
            source_repo_key="cancellation-api",
            enabled=True,
            parameter_snapshot={"depth": "deep"},
        )

    def _pipeline_with_request(self, *, state=PipelineLaunchRequestState.DISPATCHED):
        pipeline = AISTPipeline.objects.create(
            id="dast-cancel-pipeline",
            project=self.project,
            trigger_project_version=self.pv,
            execution_type=PipelineExecutionType.DAST,
            status=AISTStatus.EXECUTING,
            run_task_id="dast-task-id",
        )
        DastExecutionState.objects.create(
            pipeline=pipeline,
            run_id="provider-run-id",
            log_cursor=4,
            outcome=DastExecutionOutcome.RUNNING,
        )
        request = PipelineLaunchRequest.objects.create(
            project=self.project,
            execution_type=PipelineExecutionType.DAST,
            dast_binding=self.binding,
            trigger_project_version=self.pv,
            state=state,
            pipeline=pipeline,
        )
        return pipeline, request

    def test_running_cancel_persists_intent_and_signals_connector_without_releasing_capacity(self):
        pipeline, request = self._pipeline_with_request()

        with (
            patch("aist.utils.pipeline.cleanup_pipeline_containers") as cleanup,
            patch("aist.utils.pipeline._revoke_task") as revoke,
            self.captureOnCommitCallbacks(execute=True),
        ):
            stop_pipeline(pipeline)

        pipeline.refresh_from_db()
        execution_state = pipeline.dast_execution_state
        request.refresh_from_db()
        self.assertIsNotNone(execution_state.cancel_requested_at)
        self.assertEqual(execution_state.outcome, DastExecutionOutcome.STOP_PENDING)
        self.assertEqual(execution_state.run_id, "provider-run-id")
        self.assertEqual(execution_state.log_cursor, 4)
        self.assertEqual(pipeline.status, AISTStatus.EXECUTING)
        self.assertEqual(pipeline.run_task_id, "dast-task-id")
        self.assertEqual(request.state, PipelineLaunchRequestState.DISPATCHED)
        cleanup.assert_called_once_with(pipeline.id)
        revoke.assert_not_called()

    def test_cancel_before_dispatch_never_waits_for_a_provider_run(self):
        pipeline, request = self._pipeline_with_request(state=PipelineLaunchRequestState.PUBLISHED)

        with (
            patch("aist.utils.pipeline.cleanup_pipeline_containers") as cleanup,
            patch("aist.utils.pipeline._revoke_task") as revoke,
            patch("aist.utils.pipeline.finish_pipeline") as finish,
        ):
            stop_pipeline(pipeline)

        pipeline.refresh_from_db()
        execution_state = pipeline.dast_execution_state
        request.refresh_from_db()
        self.assertEqual(request.state, PipelineLaunchRequestState.CANCELLED)
        self.assertEqual(execution_state.outcome, DastExecutionOutcome.CANCELLED_BEFORE_START)
        cleanup.assert_not_called()
        revoke.assert_called_once_with("dast-task-id")
        finish.assert_called_once_with(pipeline.id, degraded=True)

    def test_cancel_after_terminal_pipeline_is_an_idempotent_noop(self):
        pipeline, request = self._pipeline_with_request()
        pipeline.status = AISTStatus.FINISHED
        pipeline.run_task_id = None
        pipeline.save(update_fields=["status", "run_task_id", "updated"])
        execution_state = pipeline.dast_execution_state
        execution_state.outcome = DastExecutionOutcome.TERMINAL
        execution_state.save(update_fields=["outcome", "updated"])

        with (
            patch("aist.utils.pipeline.cleanup_pipeline_containers") as cleanup,
            patch("aist.utils.pipeline._revoke_task") as revoke,
        ):
            stop_pipeline(pipeline)

        pipeline.refresh_from_db()
        execution_state.refresh_from_db()
        request.refresh_from_db()
        self.assertIsNone(execution_state.cancel_requested_at)
        self.assertEqual(execution_state.outcome, DastExecutionOutcome.TERMINAL)
        self.assertEqual(request.state, PipelineLaunchRequestState.DISPATCHED)
        cleanup.assert_not_called()
        revoke.assert_not_called()

    def test_confirmed_provider_stop_is_terminal_before_request_and_lease_release(self):
        pipeline, request = self._pipeline_with_request()

        with patch("aist.tasks.pipeline.finish_pipeline") as finish:
            pipeline_tasks._finish_dast_cancellation(pipeline.id)

        request.refresh_from_db()
        self.assertEqual(request.state, PipelineLaunchRequestState.CANCELLED)
        finish.assert_called_once_with(pipeline.id, degraded=True)

    def test_reader_and_cross_org_user_cannot_request_remote_stop(self):
        own_pipeline, _request = self._pipeline_with_request()
        foreign_pipeline = AISTPipeline.objects.create(
            id="foreign-dast-cancel",
            project=self.other_project,
            trigger_project_version=self.other_pv,
            execution_type=PipelineExecutionType.DAST,
            status=AISTStatus.EXECUTING,
        )
        reader_role, _created = Role.objects.get_or_create(
            id=Roles.Reader,
            defaults={"name": "Reader"},
        )
        Product_Type_Member.objects.filter(product_type=self.prod_type, user=self.user).update(role=reader_role)

        with patch("aist.api.pipelines.stop_pipeline") as stop:
            own_response = self.client.post(
                reverse("aist_api:pipeline_stop", kwargs={"pipeline_id": own_pipeline.id}),
            )
            foreign_response = self.client.post(
                reverse("aist_api:pipeline_stop", kwargs={"pipeline_id": foreign_pipeline.id}),
            )

        self.assertEqual(own_response.status_code, 404)
        self.assertEqual(foreign_response.status_code, 404)
        stop.assert_not_called()

    def test_read_only_pat_cannot_request_remote_stop(self):
        pipeline, _request = self._pipeline_with_request()
        _token, raw = AISTApiToken.issue(
            user=self.user,
            organization=self.organization,
            name="dast-cancel-read-only",
            scope=ApiTokenScope.READ_ONLY,
        )
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {raw}")

        with patch("aist.api.pipelines.stop_pipeline") as stop:
            response = client.post(
                reverse("aist_api:pipeline_stop", kwargs={"pipeline_id": pipeline.id}),
            )

        self.assertEqual(response.status_code, 403)
        stop.assert_not_called()
