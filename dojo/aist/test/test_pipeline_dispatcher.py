# dojo/aist/test/test_pipeline_dispatcher.py
from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import patch

from django.utils import timezone

from dojo.aist.models import (
    AISTPipeline,
    AISTProjectLaunchConfig,
    LaunchSchedule,
    PipelineLaunchQueue,
)
from dojo.aist.tasks.pipeline_dispatcher import dispatch_queued_pipelines
from dojo.aist.test.test_api import AISTApiBase


def _fake_current_app(active_map):
    # current_app.control.inspect().active() -> active_map
    inspect = SimpleNamespace(active=lambda: active_map)
    control = SimpleNamespace(inspect=lambda: inspect)
    return SimpleNamespace(control=control)


class DispatchQueuedPipelinesTests(AISTApiBase):
    def _mk_cfg_sched_and_queue(self, *, enabled=True, limit=1, dispatched=False, with_schedule=True):
        cfg = AISTProjectLaunchConfig.objects.create(
            project=self.project,
            name=f"Preset-{uuid.uuid4().hex}",
            params={"project_version": {"id": self.pv.id}, "log_level": "INFO"},
            is_default=True,
        )
        sched = None
        if with_schedule:
            sched = LaunchSchedule.objects.create(
                cron_expression="*/5 * * * *",
                enabled=enabled,
                max_concurrent_per_worker=limit,
                launch_config=cfg,
            )

        q = PipelineLaunchQueue.objects.create(
            project=self.project,
            schedule=sched,
            launch_config=cfg,
            dispatched=dispatched,
        )
        return cfg, sched, q

    @patch("dojo.aist.tasks.pipeline_dispatcher.current_app")
    @patch("dojo.aist.tasks.pipeline_dispatcher.run_sast_pipeline")
    @patch("dojo.aist.tasks.pipeline_dispatcher.create_pipeline_object")
    @patch("dojo.aist.tasks.pipeline_dispatcher.PipelineArguments.normalize_params")
    def test_skips_items_without_schedule_or_disabled(
        self, mock_norm, mock_create_pipeline, mock_run_task, mock_current_app,
    ):
        # queue without schedule
        self._mk_cfg_sched_and_queue(with_schedule=False)
        # queue with disabled schedule
        self._mk_cfg_sched_and_queue(with_schedule=True, enabled=False)

        mock_current_app.control.inspect.return_value.active.return_value = {"w1": []}

        dispatch_queued_pipelines()

        self.assertEqual(AISTPipeline.objects.count(), 0)
        self.assertEqual(PipelineLaunchQueue.objects.filter(dispatched=True).count(), 0)
        mock_norm.assert_not_called()
        mock_create_pipeline.assert_not_called()
        mock_run_task.delay.assert_not_called()

    @patch("dojo.aist.tasks.pipeline_dispatcher.current_app")
    @patch("dojo.aist.tasks.pipeline_dispatcher.run_sast_pipeline")
    @patch("dojo.aist.tasks.pipeline_dispatcher.create_pipeline_object")
    @patch("dojo.aist.tasks.pipeline_dispatcher.PipelineArguments.normalize_params")
    def test_limit_zero_dispatches_all_fifo(
        self, mock_norm, mock_create_pipeline, mock_run_task, mock_current_app,
    ):
        _, _sched, q1 = self._mk_cfg_sched_and_queue(limit=1)
        _, _, q2 = self._mk_cfg_sched_and_queue(limit=1)

        mock_current_app.control.inspect.return_value.active.return_value = {"w1": [], "w2": []}

        # normalize_params must include project_version.id because dispatcher resolves it
        mock_norm.return_value = {"project_version": {"id": self.pv.id}}
        mock_run_task.delay.return_value = SimpleNamespace(id="celery-1")

        def _mk_pipeline(project, pv, _):
            return AISTPipeline.objects.create(
                id=f"pipe-{timezone.now().timestamp()}",
                project=project,
                project_version=pv,
                status="SAST_LAUNCHED",
            )

        mock_create_pipeline.side_effect = _mk_pipeline

        dispatch_queued_pipelines()

        self.assertEqual(PipelineLaunchQueue.objects.filter(dispatched=True).count(), 2)
        self.assertEqual(AISTPipeline.objects.count(), 2)

        # FIFO: q1 should be dispatched before q2 (created/order_by("created"))
        q1.refresh_from_db()
        q2.refresh_from_db()
        self.assertTrue(q1.dispatched)
        self.assertTrue(q2.dispatched)
        self.assertIsNotNone(q1.dispatched_at)
        self.assertIsNotNone(q2.dispatched_at)

    @patch("dojo.aist.tasks.pipeline_dispatcher.current_app")
    @patch("dojo.aist.tasks.pipeline_dispatcher.logger")
    @patch("dojo.aist.tasks.pipeline_dispatcher.run_sast_pipeline")
    @patch("dojo.aist.tasks.pipeline_dispatcher.create_pipeline_object")
    @patch("dojo.aist.tasks.pipeline_dispatcher.PipelineArguments.normalize_params")
    def test_limit_with_empty_worker_inspection_does_not_block(
        self, mock_norm, mock_create_pipeline, mock_run_task, mock_logger, mock_current_app,
    ):
        # limit > 0, but inspect.active() returned None/{} => running_per_worker is empty
        _, _sched, q = self._mk_cfg_sched_and_queue(limit=2)

        mock_current_app.control.inspect.return_value.active.return_value = None

        mock_norm.return_value = {"project_version": {"id": self.pv.id}}
        mock_run_task.delay.return_value = SimpleNamespace(id="celery-x")

        mock_create_pipeline.side_effect = lambda project, pv, _: AISTPipeline.objects.create(
            id="pipe-x",
            project=project,
            project_version=pv,
            status="SAST_LAUNCHED",
        )

        dispatch_queued_pipelines()

        q.refresh_from_db()
        self.assertTrue(q.dispatched)
        mock_logger.warning.assert_called()

    @patch("dojo.aist.tasks.pipeline_dispatcher.current_app")
    @patch("dojo.aist.tasks.pipeline_dispatcher.run_sast_pipeline")
    @patch("dojo.aist.tasks.pipeline_dispatcher.create_pipeline_object")
    @patch("dojo.aist.tasks.pipeline_dispatcher.PipelineArguments.normalize_params")
    def test_limit_blocks_when_all_workers_at_capacity_stops_cycle(
        self, mock_norm, mock_create_pipeline, mock_run_task, mock_current_app,
    ):
        # limit=1, and both workers already have 1 active task -> dispatcher should stop and not dispatch
        self._mk_cfg_sched_and_queue(limit=1)
        self._mk_cfg_sched_and_queue(limit=1)

        active_map = {
            "w1": [{"name": "dojo.aist.tasks.pipeline.run_sast_pipeline"}],
            "w2": [{"name": "dojo.aist.tasks.pipeline.run_sast_pipeline"}],
        }
        mock_current_app.control.inspect.return_value.active.return_value = active_map

        dispatch_queued_pipelines()

        self.assertEqual(PipelineLaunchQueue.objects.filter(dispatched=True).count(), 0)
        mock_norm.assert_not_called()
        mock_create_pipeline.assert_not_called()
        mock_run_task.delay.assert_not_called()

    @patch("dojo.aist.tasks.pipeline_dispatcher.current_app")
    @patch("dojo.aist.tasks.pipeline_dispatcher.logger")
    @patch("dojo.aist.tasks.pipeline_dispatcher.run_sast_pipeline")
    @patch("dojo.aist.tasks.pipeline_dispatcher.create_pipeline_object")
    @patch("dojo.aist.tasks.pipeline_dispatcher.PipelineArguments.normalize_params")
    def test_params_build_failure_keeps_item_undispatched_and_continues(
        self, mock_norm, mock_create_pipeline, mock_run_task, mock_logger, mock_current_app,
    ):
        # First item fails normalization, second should still dispatch
        _, _, q1 = self._mk_cfg_sched_and_queue(limit=1)
        _, _, q2 = self._mk_cfg_sched_and_queue(limit=1)

        mock_current_app.control.inspect.return_value.active.return_value = {"w1": []}

        def _norm_side_effect(*args, **kwargs):
            # first call fails, second succeeds
            if not hasattr(_norm_side_effect, "called"):
                _norm_side_effect.called = True
                msg = "boom"
                raise ValueError(msg)
            return {"project_version": {"id": self.pv.id}}

        mock_norm.side_effect = _norm_side_effect
        mock_run_task.delay.return_value = SimpleNamespace(id="celery-ok")
        mock_create_pipeline.side_effect = lambda project, pv, _: AISTPipeline.objects.create(
            id=f"pipe-{timezone.now().timestamp()}",
            project=project,
            project_version=pv,
            status="SAST_LAUNCHED",
        )

        dispatch_queued_pipelines()

        q1.refresh_from_db()
        q2.refresh_from_db()
        self.assertFalse(q1.dispatched)
        self.assertTrue(q2.dispatched)
        mock_logger.exception.assert_called()

    @patch("dojo.aist.tasks.pipeline_dispatcher.current_app")
    @patch("dojo.aist.tasks.pipeline_dispatcher.run_sast_pipeline")
    @patch("dojo.aist.tasks.pipeline_dispatcher.create_pipeline_object")
    @patch("dojo.aist.tasks.pipeline_dispatcher.PipelineArguments.normalize_params")
    def test_success_updates_pipeline_task_id_and_queue_links(
        self, mock_norm, mock_create_pipeline, mock_run_task, mock_current_app,
    ):
        _cfg, _sched, q = self._mk_cfg_sched_and_queue(limit=1)

        mock_current_app.control.inspect.return_value.active.return_value = {"w1": []}

        mock_norm.return_value = {"project_version": {"id": self.pv.id}}
        mock_run_task.delay.return_value = SimpleNamespace(id="celery-777")

        pipeline = AISTPipeline.objects.create(
            id="pipe-777",
            project=self.project,
            project_version=self.pv,
            status="SAST_LAUNCHED",
        )
        mock_create_pipeline.return_value = pipeline

        dispatch_queued_pipelines()

        pipeline.refresh_from_db()
        self.assertEqual(pipeline.run_task_id, "celery-777")

        q.refresh_from_db()
        self.assertTrue(q.dispatched)
        self.assertIsNotNone(q.dispatched_at)
        self.assertEqual(q.pipeline_id, pipeline.id)


class DispatchQueueCapacityTests(AISTApiBase):
    @patch("dojo.aist.tasks.pipeline_dispatcher.run_sast_pipeline")
    @patch("dojo.aist.tasks.pipeline_dispatcher.create_pipeline_object")
    @patch("dojo.aist.tasks.pipeline_dispatcher.PipelineArguments.normalize_params")
    @patch("dojo.aist.tasks.pipeline_dispatcher.current_app")
    def test_two_items_same_time_limit_one_second_starts_after_first_finishes(
        self,
        mock_current_app,
        mock_normalize,
        mock_create_pipeline,
        mock_run_task,
    ):
        # schedule limit=1 => only one pipeline can run per worker
        cfg = AISTProjectLaunchConfig.objects.create(
            project=self.project,
            name=f"Preset-{uuid.uuid4().hex}",
            params={"project_version": {"id": self.pv.id}},
            is_default=True,
        )
        sched = LaunchSchedule.objects.create(
            cron_expression="*/5 * * * *",
            enabled=True,
            max_concurrent_per_worker=1,
            launch_config=cfg,
        )

        q1 = PipelineLaunchQueue.objects.create(project=self.project, schedule=sched, launch_config=cfg)
        q2 = PipelineLaunchQueue.objects.create(project=self.project, schedule=sched, launch_config=cfg)

        mock_normalize.return_value = {"project_version": {"id": self.pv.id}}
        mock_run_task.delay.side_effect = [SimpleNamespace(id="celery-1"), SimpleNamespace(id="celery-2")]

        # create_pipeline_object should return a saved pipeline (or at least an object with an id)
        def mk_pipe(*args, **kwargs):
            return AISTPipeline.objects.create(
                id=f"pipe-{timezone.now().timestamp()}",
                project=self.project,
                project_version=self.pv,
                status="SAST_LAUNCHED",
            )

        mock_create_pipeline.side_effect = mk_pipe

        # First call: worker is busy => no capacity => dispatch should do nothing
        # Second call: worker is free => dispatch should take first queue item
        active_1 = {"w1": [{"name": "dojo.aist.tasks.pipeline.run_sast_pipeline"}]}
        active_2 = {"w1": []}

        inspect = SimpleNamespace(active=lambda: active_1)
        mock_current_app.control.inspect.return_value = inspect

        dispatch_queued_pipelines()
        self.assertEqual(PipelineLaunchQueue.objects.filter(dispatched=True).count(), 0)

        # "first finished" => next tick has empty active list
        inspect.active = lambda: active_2

        dispatch_queued_pipelines()
        self.assertEqual(PipelineLaunchQueue.objects.filter(dispatched=True).count(), 1)

        # one more tick should dispatch the second
        dispatch_queued_pipelines()
        self.assertEqual(PipelineLaunchQueue.objects.filter(dispatched=True).count(), 2)

        # FIFO: q1 should be dispatched first
        q1.refresh_from_db()
        q2.refresh_from_db()
        self.assertTrue(q1.dispatched)
        self.assertTrue(q2.dispatched)
        self.assertLessEqual(q1.dispatched_at, q2.dispatched_at)
