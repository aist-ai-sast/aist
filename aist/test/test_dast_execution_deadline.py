"""
What ends a DAST run, and what must never end one.

Retries, reconciliation and cancellation share one predicate over two bounds: a wall-clock ceiling
a healthy run never reaches, and the absence of any sign of life. Taking long is not a fault.
"""

from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch

from celery.exceptions import Retry
from django.test import override_settings
from django.utils import timezone

from aist.execution.contracts import ProviderOperation
from aist.execution.dast import DastPipelineLaunchAdapter
from aist.execution.dast_deadlines import (
    dast_deadline_exhausted,
    dast_execution_over,
    dast_execution_timeout,
    dast_mark_final_pass,
    dast_progress_stalled,
    dast_provider_stall_timeout,
    dast_unreachable_grace,
)
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
from aist.utils.pipeline_imports import _import_sast_pipeline_package

_import_sast_pipeline_package()

from pipeline.dast.contracts import DastConnectorOutcomeState  # noqa: E402


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


class _ConnectorResult:

    """The parts of a connector result that ``_persist_dast_execution_result`` reads."""

    def __init__(self, pipeline_id: str, *, run_id: str, log_cursor: int):
        self.recovery = SimpleNamespace(
            correlation_id=pipeline_id,
            run_id=run_id,
            log_cursor=log_cursor,
        )
        self.outcome = SimpleNamespace(state=DastConnectorOutcomeState.STOP_PENDING)


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

    def test_abandoning_asks_the_provider_once_and_survives_a_failed_answer(self):
        """
        The provider is asked exactly once whether the run finished after all.

        An unreachable gateway must not become an exception, a second attempt, or a run left
        EXECUTING with its capacity slot held.
        """
        self._expire_deadline()

        with patch.object(
            pipeline_tasks,
            "_execute_dast_pipeline",
            side_effect=RuntimeError("gateway is unreachable"),
        ) as harvest:
            pipeline_tasks._retry_dast_execution(_FakeTask(), self.pipeline.id)

        self.assertEqual(harvest.call_count, 1)
        self.assertTrue(harvest.call_args.kwargs["harvest_only"])
        self.pipeline.refresh_from_db()
        self.state.refresh_from_db()
        self.lease.refresh_from_db()
        self.assertEqual(self.state.outcome, DastExecutionOutcome.UNREACHABLE)
        self.assertEqual(public_dast_outcome_code(self.pipeline), "TIMEOUT")
        self.assertIsNotNone(self.lease.released_at)

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

    def test_reconciliation_resumes_inside_the_deadline_and_closes_out_after_it(self):
        """
        A past-deadline run is not republished for the whole grace window, but gets one closing pass.

        Resuming costs a tunnel and an image pull; the one pass exists because when the worker that
        would have asked the provider a final question is gone, reconciliation is all that is left.
        """
        DastExecutionState.objects.filter(pk=self.state.pk).update(
            outcome=DastExecutionOutcome.UNREACHABLE,
        )

        self.assertTrue(DastPipelineLaunchAdapter.should_recover(self.pipeline))

        self._expire_deadline()

        self.assertTrue(DastPipelineLaunchAdapter.should_recover(self.pipeline))

        DastExecutionState.objects.filter(pk=self.state.pk).update(
            recovery_checkpoint=dast_mark_final_pass({}),
        )

        self.assertFalse(DastPipelineLaunchAdapter.should_recover(self.pipeline))

    def test_the_deadline_predicate_is_shared_by_every_decision(self):
        deadline = timezone.now() - dast_unreachable_grace() - timedelta(seconds=1)
        self.assertTrue(dast_deadline_exhausted(deadline))
        self.assertFalse(dast_deadline_exhausted(timezone.now() + timedelta(minutes=1)))
        # A run that never recorded a deadline is not silently abandoned.
        self.assertFalse(dast_deadline_exhausted(None))

    def test_a_run_past_the_old_four_hour_cap_is_no_longer_killed_for_taking_long(self):
        """Regression: a scan working for four and a half hours was stopped for taking long."""
        started = timezone.now() - timedelta(hours=4, minutes=20)
        timeout = dast_execution_timeout()

        self.assertIsNotNone(timeout)
        self.assertGreater(timeout, timedelta(hours=4, minutes=20))
        self.assertFalse(
            dast_execution_over(deadline=started + timeout, last_progress_at=timezone.now()),
        )

    @override_settings(AIST_DAST_EXECUTION_TIMEOUT_SECONDS=0)
    def test_the_ceiling_can_be_removed_and_a_run_without_one_keeps_going(self):
        self.assertIsNone(dast_execution_timeout())

        DastExecutionState.objects.filter(pk=self.state.pk).update(
            deadline=None,
            last_progress_at=timezone.now(),
            outcome=DastExecutionOutcome.UNREACHABLE,
        )

        self.assertFalse(pipeline_tasks._dast_reconciliation_exhausted(self.pipeline.id))
        self.assertTrue(DastPipelineLaunchAdapter.should_recover(self.pipeline))

    @override_settings(AIST_DAST_EXECUTION_TIMEOUT_SECONDS=0)
    def test_a_provider_that_went_quiet_ends_even_with_no_ceiling(self):
        """Removing the wall clock must not turn a dead provider into an endless retry storm."""
        stall_timeout = dast_provider_stall_timeout()
        self.assertIsNotNone(stall_timeout)
        DastExecutionState.objects.filter(pk=self.state.pk).update(
            deadline=None,
            last_progress_at=timezone.now() - stall_timeout - timedelta(seconds=1),
            outcome=DastExecutionOutcome.UNREACHABLE,
        )

        self.assertTrue(pipeline_tasks._dast_reconciliation_exhausted(self.pipeline.id))
        # Over, so no further scanning -- but the one closing pass is still owed.
        self.assertTrue(DastPipelineLaunchAdapter.should_recover(self.pipeline))
        DastExecutionState.objects.filter(pk=self.state.pk).update(
            recovery_checkpoint=dast_mark_final_pass({}),
        )
        self.assertFalse(DastPipelineLaunchAdapter.should_recover(self.pipeline))

    def test_a_provider_that_keeps_delivering_is_never_treated_as_stalled(self):
        stall_timeout = dast_provider_stall_timeout()

        self.assertFalse(dast_progress_stalled(timezone.now()))
        self.assertTrue(dast_progress_stalled(timezone.now() - stall_timeout - timedelta(seconds=1)))
        # A run persisted before this bound existed has no baseline, and absence of a baseline is
        # not evidence of death.
        self.assertFalse(dast_progress_stalled(None))

    @override_settings(AIST_DAST_PROVIDER_STALL_TIMEOUT_SECONDS=0)
    def test_the_stall_bound_can_be_removed_too(self):
        self.assertIsNone(dast_provider_stall_timeout())
        self.assertFalse(dast_progress_stalled(timezone.now() - timedelta(days=30)))

    def test_progress_is_recorded_only_when_the_provider_actually_delivered_something(self):
        """``last_progress_at`` is the stall bound's clock, so a no-op attempt must not wind it."""
        stale = timezone.now() - timedelta(hours=3)
        DastExecutionState.objects.filter(pk=self.state.pk).update(
            run_id="run-1",
            log_cursor=784,
            last_progress_at=stale,
        )

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
