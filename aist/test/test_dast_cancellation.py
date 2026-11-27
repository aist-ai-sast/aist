import tempfile
from pathlib import Path
from unittest.mock import Mock, patch

from celery.exceptions import Retry
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
from aist.tasks import dast_execution
from aist.tasks.pipeline import stop_pipeline_execution
from aist.test.test_api import AISTApiBase
from aist.test.test_dast_target_models import _integration_config, _target_wire
from aist.utils.pipeline import stop_pipeline
from aist.utils.pipeline_imports import _import_sast_pipeline_package

_import_sast_pipeline_package()

from pipeline.dast.connector import DastConnector, DastGatewayClient  # noqa: E402
from pipeline.dast.contracts import DastConnectorInput, DastLogPage, DastRunState, DastRunStatus  # noqa: E402


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
            # The sweep is dispatched, not run here: the Docker socket belongs to the worker, so a
            # stop swept in the web process reaches no container and the provider run carries on.
            patch("aist.tasks.pipeline.stop_pipeline_execution.delay") as dispatched,
            patch("aist.utils.pipeline.cleanup_pipeline_containers") as swept_here,
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
        dispatched.assert_called_once_with(pipeline.id)
        swept_here.assert_not_called()
        revoke.assert_not_called()

    def test_cancel_before_dispatch_never_waits_for_a_provider_run(self):
        pipeline, request = self._pipeline_with_request(state=PipelineLaunchRequestState.PUBLISHED)
        DastExecutionState.objects.filter(pipeline=pipeline).update(run_id=None, log_cursor=0)

        with (
            patch("aist.tasks.pipeline.stop_pipeline_execution.delay") as dispatched,
            patch("aist.utils.pipeline._revoke_task") as revoke,
            patch("aist.utils.pipeline.finish_pipeline") as finish,
        ):
            stop_pipeline(pipeline)

        pipeline.refresh_from_db()
        execution_state = pipeline.dast_execution_state
        request.refresh_from_db()
        self.assertEqual(request.state, PipelineLaunchRequestState.CANCELLED)
        self.assertEqual(execution_state.outcome, DastExecutionOutcome.CANCELLED_BEFORE_START)
        dispatched.assert_not_called()
        revoke.assert_called_once_with("dast-task-id")
        finish.assert_called_once_with(pipeline.id, degraded=True)

    def test_cancel_while_no_connector_exists_retries_and_reaches_the_replacement(self):
        pipeline, _request = self._pipeline_with_request()
        command = dast_execution.DastStartCommand(
            idempotency_key="launch-123", correlation_id=pipeline.pk, target_id="target",
            capability_revision=f"sha256:{'a' * 64}", trigger=None, parameters={},
        )
        gateway = Mock(spec=DastGatewayClient)
        gateway.status.return_value = DastRunStatus(
            run_id="provider-run-id", correlation_id=pipeline.pk, status=DastRunState.RUNNING,
            selection=None, error_code=None, result_ready=False,
        )
        gateway.logs.return_value = DastLogPage(events=(), next_cursor=4, has_more=False)
        observer_running = False
        signalled = []

        def sweep(_pipeline_id):
            if observer_running:
                signalled.append(True)

        def stop_attempt(_pipeline_id):
            with self.assertRaises(Retry):
                stop_pipeline_execution.run(pipeline.pk)

        def poll(_seconds):
            stop_attempt(pipeline.pk)
            self.assertTrue(signalled)
            raise KeyboardInterrupt

        with (
            tempfile.TemporaryDirectory() as output,
            patch("aist.tasks.pipeline.cleanup_pipeline_containers", side_effect=sweep),
            patch.object(stop_pipeline_execution, "retry", side_effect=Retry) as retry,
            patch.object(stop_pipeline_execution, "delay", side_effect=stop_attempt),
        ):
            with self.captureOnCommitCallbacks(execute=True):
                stop_pipeline(pipeline)
            gateway.stop.assert_not_called()
            retry.assert_called_once_with(countdown=15)
            observer_running = True
            connector = DastConnector(gateway=gateway, output_dir=Path(output), sleep=poll)
            connector.run(DastConnectorInput(
                gateway_url="https://dast.internal", command=command,
                recovery=dast_execution.DastRecoveryState.initial(command).for_run("provider-run-id").with_cursor(4),
                stop_requested=False,
            ))
            gateway.stop.assert_called_once_with("provider-run-id")
            self.assertEqual(retry.call_count, 2)
            pipeline.status = AISTStatus.FINISHED_WITH_WARNINGS
            pipeline.save(update_fields=["status"])
            stop_pipeline_execution.run(pipeline.pk)
            self.assertEqual(retry.call_count, 2)

    def test_cancel_after_terminal_pipeline_is_an_idempotent_noop(self):
        pipeline, request = self._pipeline_with_request()
        pipeline.status = AISTStatus.FINISHED
        pipeline.run_task_id = None
        pipeline.save(update_fields=["status", "run_task_id", "updated"])
        execution_state = pipeline.dast_execution_state
        execution_state.outcome = DastExecutionOutcome.TERMINAL
        execution_state.save(update_fields=["outcome", "updated"])

        with (
            patch("aist.tasks.pipeline.stop_pipeline_execution.delay") as dispatched,
            patch("aist.utils.pipeline._revoke_task") as revoke,
        ):
            stop_pipeline(pipeline)

        pipeline.refresh_from_db()
        execution_state.refresh_from_db()
        request.refresh_from_db()
        self.assertIsNone(execution_state.cancel_requested_at)
        self.assertEqual(execution_state.outcome, DastExecutionOutcome.TERMINAL)
        self.assertEqual(request.state, PipelineLaunchRequestState.DISPATCHED)
        dispatched.assert_not_called()
        revoke.assert_not_called()

    def test_confirmed_provider_stop_is_terminal_before_request_and_lease_release(self):
        pipeline, request = self._pipeline_with_request()

        with patch("aist.tasks.dast_execution.finish_pipeline") as finish:
            dast_execution._finish_dast_cancellation(pipeline.id)

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
