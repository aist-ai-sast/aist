from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from aist.internal_upload import ImportFailure, ImportResult
from aist.models import AISTPipeline, AISTStatus
from aist.test.analyzer_catalog import write_analyzers_config
from aist.test.pipeline_execution_helpers import SastWorkerTestMixin
from aist.test.test_api import AISTApiBase

AGENT_SECURITY_MISSING_RESULT = {
    "name": "agent-security",
    "type": "agent-bridge",
    "status": "missing_result",
    "degraded": True,
    "messages": [{"code": "missing_result", "text": "Required analyzer result file was not produced."}],
    "artifacts": {},
}


class AnalyzerOutcomesPipelineTests(SastWorkerTestMixin, AISTApiBase):
    def _run_with_common_patches(
        self,
        *,
        pipeline: AISTPipeline,
        output_dir: str,
        analyzer_config_path: str,
        upload_results: list,
        async_user=None,
        extra_patches=(),
    ) -> None:
        self.run_sast(
            pipeline,
            project_version=self.pv,
            launch_data={
                "output_dir": output_dir,
                "tmp_analyzer_config_path": analyzer_config_path,
                "analyzer_outcomes": [AGENT_SECURITY_MISSING_RESULT],
            },
            upload_results=upload_results,
            async_user=async_user,
            extra_patches=[
                patch("aist.tasks.sast_execution.build_bridge_client_from_settings", return_value=object()),
                *extra_patches,
            ],
        )

    def test_degraded_analyzer_outcome_is_persisted_before_finish(self):
        pipeline = self.make_pipeline("pipe-agent-missing", status=AISTStatus.FINISHED)
        with TemporaryDirectory() as tmp:
            output_dir = str(Path(tmp) / "out")
            Path(output_dir).mkdir()
            cfg_path = write_analyzers_config(Path(tmp), [])
            with patch("aist.services.pipeline_results.finish_pipeline") as mock_finish:
                self._run_with_common_patches(
                    pipeline=pipeline,
                    output_dir=output_dir,
                    analyzer_config_path=cfg_path,
                    upload_results=[],
                )

        mock_finish.assert_called_once_with(pipeline.id)
        pipeline.refresh_from_db()
        reasons = pipeline.launch_data.get("analyzer_degraded_reasons") or []
        self.assertEqual(reasons[0]["source"], "analyzer:agent-security")
        self.assertEqual(reasons[0]["code"], "missing_result")

    def test_degraded_analyzer_outcome_does_not_skip_postprocess_when_findings_exist(self):
        pipeline = self.make_pipeline("pipe-agent-postprocess", status=AISTStatus.FINISHED)
        dd_test, _finding = self.make_test_with_finding()
        result = ImportResult(analyzer_name="semgrep", test_id=dd_test.id)
        with TemporaryDirectory() as tmp:
            output_dir = str(Path(tmp) / "out")
            Path(output_dir).mkdir()
            with (
                patch("aist.services.pipeline_results.schedule_pipeline_postprocessing") as mock_postprocess,
                patch("aist.services.pipeline_results.finish_pipeline") as mock_finish,
            ):
                self._run_with_common_patches(
                    pipeline=pipeline,
                    output_dir=output_dir,
                    analyzer_config_path=str(Path(tmp) / "unused.yaml"),
                    upload_results=[result],
                    async_user=self.user,
                )

        mock_finish.assert_not_called()
        mock_postprocess.assert_called_once_with(pipeline.id, "INFO")

    def test_a_report_the_platform_fails_to_import_finishes_the_run_with_warnings(self):
        pipeline = self.make_pipeline("pipe-import-failed", status=AISTStatus.FINISHED)
        failed = ImportResult(
            analyzer_name="semgrep", failure_code=ImportFailure.IMPORT_FAILED, failure_message="Report import failed: ValueError",
        )

        self.run_sast(pipeline, project_version=self.pv, upload_results=[failed])

        pipeline.refresh_from_db()
        self.assertEqual(pipeline.status, AISTStatus.FINISHED_WITH_WARNINGS)
        self.assertEqual(
            pipeline.launch_data["analyzer_degraded_reasons"],
            [{"source": "analyzer:semgrep", "code": "import_failed", "message": "Report import failed: ValueError"}],
        )
