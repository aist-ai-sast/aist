from __future__ import annotations

from contextlib import ExitStack, contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from django.utils import timezone
from dojo.models import Engagement, Finding, Test, Test_Type

from aist.execution.dispatching import LaunchAcceptance
from aist.models import AISTPipeline, AISTStatus
from aist.test.pipeline_execution_helpers import run_persisted_sast_pipeline
from aist.test.test_api import AISTApiBase


class _DummyLogger:
    def info(self, *args, **kwargs):
        return None

    def warning(self, *args, **kwargs):
        return None

    def exception(self, *args, **kwargs):
        return None


@contextmanager
def _dummy_script_path_context():
    yield "aist-test-script.sh"


class AnalyzerOutcomesPipelineTests(AISTApiBase):
    def setUp(self):
        super().setUp()
        acceptance = patch(
            "aist.tasks.pipeline.accept_published_launch",
            return_value=LaunchAcceptance.ACCEPTED,
        )
        acceptance.start()
        self.addCleanup(acceptance.stop)

    def _pipeline_params(self, *, output_dir: str) -> SimpleNamespace:
        descriptor = {
            "id": self.pv.id,
            "type": self.pv.version_type,
            "excluded_paths": [],
        }
        return SimpleNamespace(
            project_version={"id": self.pv.id, "version": self.pv.version},
            project_name="test_product",
            languages=["python"],
            output_dir=output_dir,
            rebuild_images=False,
            analyzers=[],
            time_class_level="slow",
            dockerfile_path="Dockerfile",
            pipeline_src_path="/aist-src",
            additional_environments={},
            ai_mode="MANUAL",
            ai_filter_snapshot=None,
            script_path_context=_dummy_script_path_context,
            resolve_effective_project_version=lambda **_kwargs: self.pv,
            build_project_version_descriptor=lambda: descriptor,
            enrich_config=lambda: {
                "project_version_descriptor": descriptor,
                "log_level": "INFO",
            },
        )

    def _make_pipeline(self, pipeline_id: str) -> AISTPipeline:
        return AISTPipeline.objects.create(
            id=pipeline_id,
            project=self.project,
            project_version=self.pv,
            status=AISTStatus.FINISHED,
        )

    def _make_test_with_finding(self) -> tuple[Test, Finding]:
        engagement = Engagement.objects.create(
            name="Agent analyzer",
            target_start=timezone.now(),
            target_end=timezone.now(),
            product=self.product,
        )
        test_type = Test_Type.objects.create(name="agent analyzer test")
        dd_test = Test.objects.create(
            engagement=engagement,
            target_start=timezone.now(),
            target_end=timezone.now(),
            test_type=test_type,
        )
        finding = Finding.objects.create(
            test=dd_test,
            title="Agent analyzer finding",
            severity="High",
            date=timezone.now(),
            reporter=self.user,
            unique_id_from_tool="UID-AGENT-1",
        )
        return dd_test, finding

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
        patches = (
            patch(
                "aist.tasks.pipeline.PipelineArguments.from_dict",
                return_value=SimpleNamespace(sast=self._pipeline_params(output_dir=output_dir)),
            ),
            patch("aist.tasks.pipeline.AISTProjectVersion.ensure_extracted", return_value=None),
            patch("aist.tasks.pipeline.get_project_build_path", return_value="/aist-project"),
            patch("aist.tasks.pipeline.cleanup_terminal_project_build_paths", return_value=None),
            patch("aist.tasks.pipeline.install_pipeline_logging", return_value=_DummyLogger()),
            patch("aist.tasks.pipeline.build_bridge_client_from_settings", return_value=object()),
            patch(
                "aist.tasks.pipeline.execute_pipeline",
                return_value=SimpleNamespace(launch_data={
                    "git": {},
                    "output_dir": output_dir,
                    "project_path": "/aist-project",
                    "trim_path": "",
                    "tmp_analyzer_config_path": analyzer_config_path,
                    "analyzer_outcomes": [
                        {
                            "name": "agent-security",
                            "type": "agent-bridge",
                            "status": "missing_result",
                            "degraded": True,
                            "messages": [
                                {
                                    "code": "missing_result",
                                    "text": "Required analyzer result file was not produced.",
                                },
                            ],
                            "artifacts": {},
                        },
                    ],
                }),
            ),
            patch("aist.tasks.pipeline.upload_results_internal", return_value=upload_results),
            *extra_patches,
        )
        with ExitStack() as stack:
            for ctx in patches:
                stack.enter_context(ctx)
            run_persisted_sast_pipeline(
                pipeline,
                {"project_id": self.project.id},
                async_user=async_user,
            )

    def test_degraded_analyzer_outcome_is_persisted_before_finish(self):
        pipeline = self._make_pipeline("pipe-agent-missing")
        with TemporaryDirectory() as tmp:
            output_dir = str(Path(tmp) / "out")
            Path(output_dir).mkdir()
            cfg_path = Path(tmp) / "analyzers.yaml"
            cfg_path.write_text("analyzers: []\n", encoding="utf-8")
            with patch("aist.tasks.pipeline.finish_pipeline") as mock_finish:
                self._run_with_common_patches(
                    pipeline=pipeline,
                    output_dir=output_dir,
                    analyzer_config_path=str(cfg_path),
                    upload_results=[],
                )

        mock_finish.assert_called_once_with(pipeline.id)
        pipeline.refresh_from_db()
        reasons = pipeline.launch_data.get("analyzer_degraded_reasons") or []
        self.assertEqual(reasons[0]["source"], "analyzer:agent-security")
        self.assertEqual(reasons[0]["code"], "missing_result")

    def test_degraded_analyzer_outcome_does_not_skip_postprocess_when_findings_exist(self):
        pipeline = self._make_pipeline("pipe-agent-postprocess")
        dd_test, _finding = self._make_test_with_finding()
        result = SimpleNamespace(analyzer_name="semgrep", test_id=dd_test.id)
        with TemporaryDirectory() as tmp:
            output_dir = str(Path(tmp) / "out")
            Path(output_dir).mkdir()
            with (
                patch("aist.tasks.pipeline.postprocess_findings") as mock_postprocess,
                patch("aist.tasks.pipeline.finish_pipeline") as mock_finish,
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
