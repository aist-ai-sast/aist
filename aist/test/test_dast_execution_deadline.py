"""
The deadline is the only thing that ends a DAST run.

Retries, reconciliation and cancellation each used to decide on their own, which is how a run
could keep retrying past its deadline, be resurrected forever, or -- worst -- stop being retried
while nobody terminalized it, leaving the pipeline EXECUTING with its capacity lease held.
"""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import patch

from celery.exceptions import Retry
from django.utils import timezone

from aist.execution.contracts import ProviderOperation
from aist.execution.dast import DastPipelineLaunchAdapter
from aist.execution.dast_deadlines import dast_deadline_exhausted, dast_unreachable_grace
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

    """Stands in for the bound Celery task, with the retry budget already spent."""

    def __init__(self, *, retries: int = 0, exhausted: bool = False):
        self.request = type("Request", (), {"retries": retries, "id": "task-1"})()
        self._exhausted = exhausted
        self.retry_calls: list[dict] = []

    def retry(self, **kwargs):
        self.retry_calls.append(kwargs)
        if self._exhausted:
            # What Celery raises once max_retries is reached: the original error, from inside
            # the caller's own exception handler.
            detail = "provider unreachable"
            raise RuntimeError(detail)
        return Retry()


class _DastPipelineFixture(AISTApiBase):

    """A DAST pipeline whose launch request satisfies the database's binding invariants."""

    def setUp(self):
        super().setUp()
        organization = Organization.objects.create(
            name=f"Deadline org {self.id()}",
            product_type=self.prod_type,
        )
        integration, _state = dast_fixtures.create_dast_integration(
            organization=organization,
            public_id="deadline-public-id",
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


class DastExecutionDeadlineTests(_DastPipelineFixture):
    def setUp(self):
        super().setUp()
        self.pipeline = AISTPipeline.objects.create(
            id="deadline1",
            project=self.project,
            project_version=self.pv,
            execution_type=PipelineExecutionType.DAST,
            status=AISTStatus.EXECUTING,
            run_task_id="task-1",
        )
        self.state = DastExecutionState.objects.create(
            pipeline=self.pipeline,
            deadline=timezone.now() + timedelta(hours=1),
        )
        self.request = self._launch_request(self.pipeline)
        self.lease = PipelineExecutionLease.objects.create(
            pipeline=self.pipeline,
            request=self.request,
            resource_key="dast-integration:1",
            slot=0,
            expires_at=timezone.now() + timedelta(minutes=5),
        )

    def _expire_deadline(self):
        DastExecutionState.objects.filter(pk=self.state.pk).update(
            deadline=timezone.now() - dast_unreachable_grace() - timedelta(seconds=1),
        )

    def test_a_run_inside_its_deadline_is_retried(self):
        task = _FakeTask()

        # Retry propagates: that is how Celery reschedules the attempt.
        with self.assertRaises(Retry):
            pipeline_tasks._retry_dast_execution(task, self.pipeline.id)

        self.assertEqual(len(task.retry_calls), 1)
        self.pipeline.refresh_from_db()
        self.assertEqual(self.pipeline.status, AISTStatus.EXECUTING)

    def test_an_exhausted_retry_budget_still_terminalizes_and_frees_the_slot(self):
        """
        The defect this covers: Celery re-raises from inside the exception handler that asked for
        the retry, so the caller's own `except` never saw it and finish_pipeline was skipped. The
        pipeline stayed EXECUTING and its lease was never released, so the integration's only
        capacity slot was held forever.
        """
        task = _FakeTask(exhausted=True)

        pipeline_tasks._retry_dast_execution(task, self.pipeline.id)

        self.pipeline.refresh_from_db()
        self.state.refresh_from_db()
        self.lease.refresh_from_db()
        self.assertIn(
            self.pipeline.status,
            {AISTStatus.FINISHED, AISTStatus.FINISHED_WITH_WARNINGS},
        )
        self.assertEqual(self.state.outcome, DastExecutionOutcome.UNREACHABLE)
        self.assertIsNotNone(self.lease.released_at)

    def test_a_run_past_its_deadline_is_terminalized_instead_of_retried(self):
        self._expire_deadline()
        task = _FakeTask()

        pipeline_tasks._retry_dast_execution(task, self.pipeline.id)

        self.assertEqual(task.retry_calls, [])
        self.pipeline.refresh_from_db()
        self.assertIn(
            self.pipeline.status,
            {AISTStatus.FINISHED, AISTStatus.FINISHED_WITH_WARNINGS},
        )

    def test_a_connector_that_cannot_start_fails_now_instead_of_retrying_until_the_deadline(self):
        """
        A failure on this host -- an unreadable handoff, an unwritable output directory -- repeats
        identically on every attempt. Treating it as an unreachable provider spent the whole
        deadline window on it and told the user the provider was at fault.
        """
        task = _FakeTask()

        with patch.object(
            pipeline_tasks,
            "_execute_dast_pipeline",
            side_effect=pipeline_tasks.DastExecutionLocalFailure("connector could not start"),
        ):
            result = pipeline_tasks._run_dast_execution(
                task,
                self.pipeline.id,
                operation=ProviderOperation.EXECUTE,
            )

        self.assertIsNone(result)
        self.assertEqual(task.retry_calls, [])
        self.pipeline.refresh_from_db()
        self.state.refresh_from_db()
        self.lease.refresh_from_db()
        self.assertIn(
            self.pipeline.status,
            {AISTStatus.FINISHED, AISTStatus.FINISHED_WITH_WARNINGS},
        )
        self.assertEqual(public_dast_outcome_code(self.pipeline), "RUNTIME_FAILED")
        self.assertNotEqual(self.state.outcome, DastExecutionOutcome.UNREACHABLE)
        self.assertIsNotNone(self.lease.released_at)

    def test_reconciliation_resumes_inside_the_deadline_and_stops_after_it(self):
        """
        Resuming costs a VPN tunnel and an image pull every pass, so a run whose deadline has
        passed must not be republished for the rest of the grace window.
        """
        DastExecutionState.objects.filter(pk=self.state.pk).update(
            outcome=DastExecutionOutcome.UNREACHABLE,
        )

        self.assertTrue(DastPipelineLaunchAdapter.should_recover(self.pipeline))

        self._expire_deadline()

        self.assertFalse(DastPipelineLaunchAdapter.should_recover(self.pipeline))

    def test_the_deadline_predicate_is_shared_by_every_decision(self):
        deadline = timezone.now() - dast_unreachable_grace() - timedelta(seconds=1)
        self.assertTrue(dast_deadline_exhausted(deadline))
        self.assertFalse(dast_deadline_exhausted(timezone.now() + timedelta(minutes=1)))
        # A run that never recorded a deadline is not silently abandoned.
        self.assertFalse(dast_deadline_exhausted(None))


class DastStopBeforeProviderRunTests(_DastPipelineFixture):

    """Stop must not wait on a connector that owns nothing yet."""

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
        self.assertIn(
            self.pipeline.status,
            {AISTStatus.FINISHED, AISTStatus.FINISHED_WITH_WARNINGS},
        )
