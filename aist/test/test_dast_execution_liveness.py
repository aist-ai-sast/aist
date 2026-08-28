"""DAST liveness: silence is telemetry and never a business termination decision."""

from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch

from celery.exceptions import Retry
from django.utils import timezone

from aist.execution.contracts import ProviderOperation
from aist.execution.dast import DastPipelineLaunchAdapter
from aist.models import (
    AISTPipeline,
    AISTStatus,
    DastExecutionOutcome,
    DastExecutionState,
    Organization,
    PipelineExecutionLease,
    PipelineExecutionType,
    PipelineLaunchRequest,
    PipelineLaunchRequestState,
)
from aist.services.dast_outcomes import public_dast_outcome_code
from aist.tasks import pipeline as pipeline_tasks
from aist.test import dast_fixtures
from aist.test.test_api import AISTApiBase
from aist.utils.pipeline import _request_dast_pipeline_stop


class _FakeTask:
    def __init__(self, *, retries: int = 0):
        self.request = type("Request", (), {"retries": retries, "id": "task-1"})()
        self.retry_calls: list[dict] = []

    def retry(self, **kwargs):
        self.retry_calls.append(kwargs)
        return Retry()


class _ConnectorResult:
    def __init__(self, pipeline_id: str, *, run_id: str, log_cursor: int):
        self.recovery = SimpleNamespace(
            correlation_id=pipeline_id,
            run_id=run_id,
            log_cursor=log_cursor,
        )
        self.outcome = SimpleNamespace(state=pipeline_tasks.DastConnectorOutcomeState.STOP_PENDING)


class _DastPipelineFixture(AISTApiBase):
    def setUp(self):
        super().setUp()
        organization = Organization.objects.create(name=f"Liveness org {self.id()}", product_type=self.prod_type)
        integration, _state = dast_fixtures.create_dast_integration(
            organization=organization,
            public_id="liveness-public-id",
        )
        target = dast_fixtures.create_dast_target(
            integration=integration,
            wire=dast_fixtures.perimeter_target_wire(),
        )
        self.binding = dast_fixtures.create_dast_binding(project=self.project, target=target)

    def _launch_request(self, pipeline, *, state=PipelineLaunchRequestState.DISPATCHED):
        return PipelineLaunchRequest.objects.create(
            origin="USER",
            execution_type=PipelineExecutionType.DAST,
            project=self.project,
            pipeline=pipeline,
            state=state,
            authority_kind="USER",
            coalesce_key=f"{pipeline.id}-key",
            dast_binding=self.binding,
        )


class DastExecutionLivenessTests(_DastPipelineFixture):
    def setUp(self):
        super().setUp()
        self.pipeline = AISTPipeline.objects.create(
            id="liveness1",
            project=self.project,
            project_version=self.pv,
            execution_type=PipelineExecutionType.DAST,
            status=AISTStatus.EXECUTING,
            run_task_id="task-1",
        )
        self.state = DastExecutionState.objects.create(
            pipeline=self.pipeline,
            outcome=DastExecutionOutcome.UNREACHABLE,
            last_progress_at=timezone.now() - timedelta(days=30),
        )
        self.request = self._launch_request(self.pipeline)
        self.lease = PipelineExecutionLease.objects.create(
            pipeline=self.pipeline,
            request=self.request,
            resource_key="dast-integration:1",
            slot=0,
            expires_at=timezone.now() + timedelta(minutes=5),
        )

    def test_provider_silence_retries_without_terminalizing_or_releasing_capacity(self):
        task = _FakeTask(retries=20)

        with self.assertRaises(Retry):
            pipeline_tasks._retry_dast_execution(task, self.pipeline.id)

        self.pipeline.refresh_from_db()
        self.lease.refresh_from_db()
        self.assertEqual(self.pipeline.status, AISTStatus.EXECUTING)
        self.assertIsNone(self.lease.released_at)
        self.assertIsNone(public_dast_outcome_code(self.pipeline))
        self.assertEqual(task.retry_calls[0]["countdown"], 300)

    def test_reconciliation_always_recovers_an_unfinished_provider_run(self):
        self.assertTrue(DastPipelineLaunchAdapter.should_recover(self.pipeline))
        DastExecutionState.objects.filter(pk=self.state.pk).update(
            recovery_checkpoint={"obsolete_final_attempt_at": "2026-01-01T00:00:00Z"},
        )
        self.assertTrue(DastPipelineLaunchAdapter.should_recover(self.pipeline))

    def test_last_progress_is_telemetry_only_and_advances_only_on_delivery(self):
        stale = self.state.last_progress_at
        DastExecutionState.objects.filter(pk=self.state.pk).update(run_id="run-1", log_cursor=784)

        pipeline_tasks._persist_dast_execution_result(
            self.pipeline.id,
            _ConnectorResult(self.pipeline.id, run_id="run-1", log_cursor=784),
        )
        self.state.refresh_from_db()
        self.assertEqual(self.state.last_progress_at, stale)

        pipeline_tasks._persist_dast_execution_result(
            self.pipeline.id,
            _ConnectorResult(self.pipeline.id, run_id="run-1", log_cursor=791),
        )
        self.state.refresh_from_db()
        self.assertGreater(self.state.last_progress_at, stale)

    def test_local_connector_setup_failure_remains_an_explicit_terminal_outcome(self):
        task = _FakeTask()
        with patch.object(
            pipeline_tasks,
            "_execute_dast_pipeline",
            side_effect=pipeline_tasks.DastExecutionLocalFailure("connector could not start"),
        ):
            result = pipeline_tasks._run_dast_execution(task, self.pipeline.id, operation=ProviderOperation.EXECUTE)

        self.assertIsNone(result)
        self.pipeline.refresh_from_db()
        self.lease.refresh_from_db()
        self.assertEqual(public_dast_outcome_code(self.pipeline), "RUNTIME_FAILED")
        self.assertIsNotNone(self.lease.released_at)


class DastStopBeforeProviderRunTests(_DastPipelineFixture):
    def setUp(self):
        super().setUp()
        self.pipeline = AISTPipeline.objects.create(
            id="stopearly",
            project=self.project,
            project_version=self.pv,
            execution_type=PipelineExecutionType.DAST,
            status=AISTStatus.ADMITTED,
        )
        DastExecutionState.objects.create(pipeline=self.pipeline)
        self.request = self._launch_request(self.pipeline)

    def test_stop_with_no_provider_run_and_no_worker_completes_locally(self):
        with patch("aist.utils.pipeline.cleanup_pipeline_containers"):
            _request_dast_pipeline_stop(self.pipeline.id)

        self.pipeline.refresh_from_db()
        self.request.refresh_from_db()
        state = DastExecutionState.objects.get(pipeline=self.pipeline)
        self.assertEqual(state.outcome, DastExecutionOutcome.CANCELLED_BEFORE_START)
        self.assertEqual(self.request.state, PipelineLaunchRequestState.CANCELLED)
        self.assertIn(self.pipeline.status, {AISTStatus.FINISHED, AISTStatus.FINISHED_WITH_WARNINGS})
