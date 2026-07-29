from unittest.mock import patch

from aist.execution.dispatching import LaunchAcceptance
from aist.models import AISTPipeline, PipelineExecutionType, PipelineLaunchRequest
from aist.tasks.pipeline import run_pipeline_execution
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
            patch("aist.tasks.pipeline.install_pipeline_logging") as install_logging,
            patch("aist.tasks.pipeline._execute_dast_pipeline") as execute,
        ):
            run_pipeline_execution.run(pipeline.pk)

        execute.assert_called_once_with(pipeline.pk, install_logging.return_value)

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
            patch("aist.tasks.pipeline.install_pipeline_logging") as install_logging,
            patch("aist.tasks.pipeline._execute_sast_pipeline") as execute,
        ):
            run_pipeline_execution.run(pipeline.pk)

        execute.assert_called_once_with(
            pipeline.pk,
            request.params_snapshot,
            "INFO",
            None,
            install_logging.return_value,
            None,
        )

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
