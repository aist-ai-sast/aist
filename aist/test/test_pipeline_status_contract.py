from types import SimpleNamespace

from django.template.loader import render_to_string

from aist.models import AISTPipeline, AISTStatus
from aist.services.pipeline_lifecycle import (
    ACTIVE_PIPELINE_STATUSES,
    SUCCESSFUL_PIPELINE_STATUSES,
    TERMINAL_PIPELINE_STATUSES,
    is_terminal_pipeline_status,
)
from aist.test.test_api import AISTApiBase
from aist.utils.pipeline import get_terminal_pipeline_statuses, has_unfinished_pipeline


class PipelineStatusContractTests(AISTApiBase):

    """Consumer matrix for provider-neutral lifecycle status semantics."""

    def test_status_taxonomy_is_shared_by_terminal_consumers(self):
        expected = {
            AISTStatus.ADMITTED: False,
            AISTStatus.EXECUTING: False,
            AISTStatus.UPLOADING_RESULTS: False,
            AISTStatus.FINDING_POSTPROCESSING: False,
            AISTStatus.WAITING_DEDUPLICATION_TO_FINISH: False,
            AISTStatus.WAITING_CONFIRMATION_TO_PUSH_TO_AI: False,
            AISTStatus.PUSH_TO_AI: False,
            AISTStatus.WAITING_RESULT_FROM_AI: False,
            AISTStatus.FINISHED: True,
            AISTStatus.FINISHED_WITH_WARNINGS: True,
        }

        self.assertEqual(set(TERMINAL_PIPELINE_STATUSES), set(get_terminal_pipeline_statuses()))
        self.assertEqual(set(SUCCESSFUL_PIPELINE_STATUSES), set(TERMINAL_PIPELINE_STATUSES))
        self.assertEqual(set(ACTIVE_PIPELINE_STATUSES), {status for status, terminal in expected.items() if not terminal})
        for status, terminal in expected.items():
            with self.subTest(status=status):
                self.assertEqual(is_terminal_pipeline_status(status), terminal)

    def test_admission_and_execution_are_unfinished_but_terminal_outcomes_are_not(self):
        for status, unfinished in (
            (AISTStatus.ADMITTED, True),
            (AISTStatus.EXECUTING, True),
            (AISTStatus.FINISHED, False),
            (AISTStatus.FINISHED_WITH_WARNINGS, False),
        ):
            with self.subTest(status=status):
                pipeline = AISTPipeline.objects.create(
                    id=f"status-contract-{status.lower()}",
                    project=self.project,
                    project_version=self.pv,
                    status=status,
                )
                self.assertEqual(has_unfinished_pipeline(self.pv), unfinished)
                pipeline.delete()

    def test_pipeline_detail_renders_admission_and_execution_states(self):
        admitted = render_to_string(
            "aist/_pipeline_status_container.html",
            {"pipeline": SimpleNamespace(status=AISTStatus.ADMITTED)},
        )
        executing = render_to_string(
            "aist/_pipeline_status_container.html",
            {"pipeline": SimpleNamespace(status=AISTStatus.EXECUTING)},
        )

        self.assertIn("Waiting for execution capacity", admitted)
        self.assertIn("The pipeline worker is running", executing)
