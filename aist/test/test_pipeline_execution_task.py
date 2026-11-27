import time
from math import inf
from unittest.mock import Mock, patch

from billiard.einfo import ExceptionWithTraceback
from billiard.exceptions import TimeLimitExceeded
from billiard.pool import Pool
from celery import states
from celery.backends.cache import CacheBackend
from celery.exceptions import Ignore, Retry
from celery.worker.request import Request, create_request_cls
from django.conf import settings
from kombu import Message
from kombu.serialization import dumps, loads

from aist.execution.contracts import ProviderOperation
from aist.execution.dispatching import LaunchAcceptance
from aist.models import AISTPipeline, DastExecutionState, PipelineExecutionType, PipelineLaunchRequest
from aist.tasks.dast_execution import DastExecutionIncomplete
from aist.tasks.pipeline import run_pipeline_execution, stop_pipeline_execution
from aist.test.test_api import AISTApiBase


class PipelineExecutionTaskTests(AISTApiBase):
    def _dast_pipeline(self):
        return AISTPipeline.objects.create(
            id="dast-task-boundary",
            project=self.project,
            trigger_project_version=self.pv,
            execution_type=PipelineExecutionType.DAST,
        )

    def test_generic_task_loads_persisted_dast_type_and_invokes_executor_boundary(self):
        pipeline = self._dast_pipeline()

        with (
            patch(
                "aist.tasks.pipeline.accept_published_launch",
                return_value=LaunchAcceptance.ACCEPTED,
            ),
            patch("aist.tasks.dast_execution.install_pipeline_logging") as install_logging,
            patch("aist.tasks.dast_execution._execute_dast_pipeline") as execute,
        ):
            run_pipeline_execution.run(pipeline.pk)

        execute.assert_called_once_with(pipeline.pk, install_logging.return_value)

    def test_pipeline_limits_survive_broker_worker_and_prefork_pool_defaults(self):
        app = run_pipeline_execution.app
        pipeline_limit = settings.AIST_PIPELINE_TASK_TIME_LIMIT or inf
        for task in (run_pipeline_execution, stop_pipeline_execution):
            with self.subTest(task=task.name):
                message = app.amqp.create_task_message(
                    "limit-test", task.name, args=["pipeline-id"],
                    time_limit=task.time_limit, soft_time_limit=task.soft_time_limit,
                )
                content_type, encoding, serialized = dumps(message.headers, serializer="json")
                headers = loads(serialized, content_type, encoding)
                pool = Mock()
                request_cls = create_request_cls(Request, task, pool, "test-worker", None, app=app)
                request = request_cls(
                    Message(headers=headers, body=message.body), app=app, task=task, decoded=True,
                )
                request.execute_using_pool(pool)
                timeout = pool.apply_async.call_args.kwargs["timeout"]
                # Compress the inherited 12-hour deadline to one second and outlive it.
                with Pool(processes=1, timeout=1) as prefork:
                    result = prefork.apply_async(
                        time.sleep, (2.5,), timeout=timeout,
                        soft_timeout=pool.apply_async.call_args.kwargs["soft_timeout"],
                    )
                    if task is run_pipeline_execution and pipeline_limit > 2.5:
                        self.assertIsNone(result.get(timeout=10))
                    else:
                        with self.assertRaises(ExceptionWithTraceback) as raised:
                            result.get(timeout=10)
                        self.assertIsInstance(raised.exception.exc, TimeLimitExceeded)

    def test_dast_retry_keeps_its_limits_and_task_identity(self):
        pipeline = self._dast_pipeline()
        DastExecutionState.objects.create(pipeline=pipeline, run_id="live-run", log_cursor=17)
        task = run_pipeline_execution
        message = task.app.amqp.create_task_message(
            "retry-id", task.name, args=[pipeline.pk], retries=1,
            time_limit=task.time_limit, soft_time_limit=task.soft_time_limit,
        )
        task.push_request(**message.headers, args=(pipeline.pk,), called_directly=False, is_eager=False)
        try:
            with (
                patch("aist.tasks.pipeline.accept_published_launch", return_value=LaunchAcceptance.ACCEPTED),
                patch("aist.tasks.dast_execution.install_pipeline_logging"),
                patch("aist.tasks.dast_execution._execute_dast_pipeline", side_effect=DastExecutionIncomplete),
                patch.object(task.app, "send_task") as published,
                self.assertRaises(Retry),
            ):
                task.run(pipeline.pk)
        finally:
            task.pop_request()
        self.assertEqual(published.call_args.args[0], task.name)
        self.assertEqual(published.call_args.kwargs["task_id"], "retry-id")
        self.assertEqual(published.call_args.kwargs["time_limit"], settings.AIST_PIPELINE_TASK_TIME_LIMIT or inf)
        self.assertEqual(published.call_args.kwargs["soft_time_limit"], 0)

    def test_duplicate_delivery_does_not_overwrite_live_task_result(self):
        pipeline = self._dast_pipeline()
        backend = CacheBackend(app=run_pipeline_execution.app, backend="memory")
        backend.store_result("live-task", None, states.STARTED)
        original_backend = run_pipeline_execution.backend
        run_pipeline_execution.backend = backend
        try:
            with (
                patch.object(run_pipeline_execution, "store_eager_result", new=True),
                patch("aist.tasks.pipeline.accept_published_launch", return_value=LaunchAcceptance.DUPLICATE),
                patch("aist.tasks.pipeline.run_dast_execution") as execute,
            ):
                result = run_pipeline_execution.apply(args=(pipeline.pk,), task_id="live-task")
        finally:
            run_pipeline_execution.backend = original_backend
        self.assertEqual(result.state, states.IGNORED)
        self.assertEqual(backend.get_state("live-task"), states.STARTED)
        execute.assert_not_called()

    def test_late_delivery_after_pipeline_deletion_is_ignored(self):
        with (
            self.assertLogs("aist.tasks.pipeline", level="INFO"),
            self.assertRaises(Ignore),
        ):
            run_pipeline_execution.run("deleted-pipeline")

    def test_republished_dast_execution_resumes_saved_provider_identity(self):
        pipeline = self._dast_pipeline()
        state = DastExecutionState.objects.create(pipeline=pipeline, run_id="live-run", log_cursor=17)
        with (
            patch("aist.tasks.pipeline.accept_published_launch", return_value=LaunchAcceptance.ACCEPTED),
            patch("aist.tasks.pipeline.run_dast_execution") as execute,
        ):
            run_pipeline_execution.run(pipeline.pk)
        execute.assert_called_once_with(run_pipeline_execution, pipeline.pk, operation=ProviderOperation.RESUME)
        state.refresh_from_db()
        self.assertEqual((state.run_id, state.log_cursor), ("live-run", 17))

    def test_generic_task_loads_persisted_sast_snapshot(self):
        pipeline = AISTPipeline.objects.create(
            id="sast-task-boundary",
            project=self.project,
            project_version=self.pv,
            execution_type=PipelineExecutionType.SAST,
        )
        request = PipelineLaunchRequest.objects.create(
            project=self.project,
            params_snapshot={"project_version": self.pv.as_dict(), "log_level": "INFO"},
            pipeline=pipeline,
        )

        with (
            patch("aist.tasks.pipeline.accept_published_launch", return_value=LaunchAcceptance.ACCEPTED),
            patch("aist.tasks.sast_execution.install_pipeline_logging"),
            patch("aist.tasks.sast_execution._execute_sast_pipeline") as execute,
        ):
            run_pipeline_execution.run(pipeline.pk)

        run, params = execute.call_args.args
        self.assertEqual((run.pipeline_id, params), (pipeline.pk, request.params_snapshot))

    def test_generic_task_rejects_non_executable_persisted_type_before_acceptance(self):
        pipeline = AISTPipeline.objects.create(
            id="manual-task-boundary",
            project=self.project,
            execution_type=PipelineExecutionType.MANUAL_IMPORT,
        )

        with (
            patch("aist.tasks.pipeline.accept_published_launch") as accept,
            self.assertRaisesRegex(ValueError, "not executable"),
        ):
            run_pipeline_execution.run(pipeline.pk)

        accept.assert_not_called()
