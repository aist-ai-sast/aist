from unittest.mock import patch

from aist.execution.dispatching import LaunchAcceptance
from aist.models import AISTPipeline, PipelineExecutionType
from aist.tasks.pipeline import run_pipeline_execution, run_sast_pipeline
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

    def test_compatibility_task_rejects_dast_before_launch_acceptance(self):
        pipeline = self._dast_pipeline()

        with (
            patch("aist.tasks.pipeline.accept_published_launch") as accept,
            self.assertRaisesRegex(ValueError, "only persisted SAST"),
        ):
            run_sast_pipeline.run(pipeline.pk, {})

        accept.assert_not_called()

    def test_generic_task_rejects_sast_before_launch_acceptance(self):
        pipeline = AISTPipeline.objects.create(
            id="sast-task-boundary",
            project=self.project,
            project_version=self.pv,
            execution_type=PipelineExecutionType.SAST,
        )

        with (
            patch("aist.tasks.pipeline.accept_published_launch") as accept,
            self.assertRaisesRegex(ValueError, "only persisted DAST"),
        ):
            run_pipeline_execution.run(pipeline.pk)

        accept.assert_not_called()
