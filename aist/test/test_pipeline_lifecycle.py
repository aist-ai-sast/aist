from __future__ import annotations

from datetime import timedelta

from django.utils import timezone

from aist.models import (
    AISTPipeline,
    AISTStatus,
    PipelineExecutionLease,
    PipelineLaunchRequest,
)
from aist.services.pipeline_lifecycle import PipelineTransitionError, transition_pipeline_status
from aist.test.test_api import AISTApiBase


class PipelineLifecycleTests(AISTApiBase):
    def _pipeline(self, *, status=AISTStatus.ADMITTED):
        return AISTPipeline.objects.create(
            id=f"lifecycle-{AISTPipeline.objects.count()}",
            project=self.project,
            project_version=self.pv,
            status=status,
            run_task_id="worker-1",
        )

    def test_admitted_executing_and_terminal_timestamps_are_truthful(self):
        pipeline = self._pipeline()
        self.assertIsNone(pipeline.started)
        self.assertIsNone(pipeline.finished_at)

        executing = transition_pipeline_status(pipeline.pk, AISTStatus.EXECUTING)
        self.assertTrue(executing.changed)
        self.assertIsNotNone(executing.pipeline.started)
        self.assertIsNone(executing.pipeline.finished_at)

        duplicate = transition_pipeline_status(pipeline.pk, AISTStatus.EXECUTING)
        self.assertFalse(duplicate.changed)
        terminal = transition_pipeline_status(pipeline.pk, AISTStatus.FINISHED)
        self.assertIsNotNone(terminal.pipeline.finished_at)
        self.assertIsNone(terminal.pipeline.run_task_id)

    def test_terminal_pipeline_cannot_return_to_active_state_and_releases_lease(self):
        pipeline = self._pipeline(status=AISTStatus.EXECUTING)
        request = PipelineLaunchRequest.objects.create(
            project=self.project,
            params_snapshot={"project_version": {"id": self.pv.pk}},
        )
        lease = PipelineExecutionLease.objects.create(
            resource_key="lifecycle:test",
            slot=1,
            request=request,
            pipeline=pipeline,
            expires_at=timezone.now() + timedelta(minutes=15),
        )

        transition_pipeline_status(pipeline.pk, AISTStatus.FINISHED_WITH_WARNINGS)
        lease.refresh_from_db()
        self.assertIsNotNone(lease.released_at)
        with self.assertRaises(PipelineTransitionError):
            transition_pipeline_status(pipeline.pk, AISTStatus.EXECUTING)
