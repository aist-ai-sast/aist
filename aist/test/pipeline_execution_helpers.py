import logging
import tempfile
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import patch

from django.utils import timezone
from dojo.models import Engagement, Finding, Test, Test_Type

from aist.execution.dispatching import LaunchAcceptance
from aist.models import AISTStatus, PipelineLaunchRequest, PipelineLaunchRequestState
from aist.pipeline_args import PipelineArguments
from aist.tasks.pipeline import run_pipeline_execution

# What the analyzer run reports when a test does not say otherwise.
ANALYZER_RUN_LAUNCH_DATA = {
    "git": {},
    "output_dir": "/aist-output",
    "project_path": "/aist-project",
    "trim_path": "",
    "tmp_analyzer_config_path": "/aist-analyzers.yml",
}


def run_persisted_sast_pipeline(pipeline, params, *, async_user=None):
    """Admit ``params`` as a SAST request of ``pipeline`` and run it through the generic worker boundary."""
    PipelineLaunchRequest.objects.update_or_create(
        pipeline=pipeline,
        defaults={
            "project": pipeline.project,
            "requester": async_user,
            "params_snapshot": PipelineArguments.normalize_params(project=pipeline.project, raw_params=dict(params)),
            "state": PipelineLaunchRequestState.PUBLISHED,
            "task_name": run_pipeline_execution.name,
            "task_args_snapshot": [],
        },
    )
    type(pipeline).objects.filter(pk=pipeline.pk).update(status=AISTStatus.EXECUTING)
    pipeline.status = AISTStatus.EXECUTING
    return run_pipeline_execution.run(pipeline.pk)


class SastWorkerTestMixin:

    """
    Runs persisted SAST launches through the worker with what lies outside the database stubbed.

    Worker acceptance is granted, build and output directories live in a per-test temporary directory, and
    :meth:`run_sast` stubs archive extraction, the analyzer run and, when asked, the report upload.
    """

    def setUp(self):
        super().setUp()
        acceptance = patch("aist.tasks.pipeline.accept_published_launch", return_value=LaunchAcceptance.ACCEPTED)
        acceptance.start()
        self.addCleanup(acceptance.stop)
        runtime_dir = tempfile.TemporaryDirectory()
        self.addCleanup(runtime_dir.cleanup)
        settings_override = self.settings(
            AIST_PROJECTS_BUILD_DIR=f"{runtime_dir.name}/build",
            AIST_OUTPUT_PATH=f"{runtime_dir.name}/output",
        )
        settings_override.enable()
        self.addCleanup(settings_override.disable)

    def run_sast(
        self,
        pipeline,
        *,
        project_version,
        launch_data=None,
        upload_results=None,
        extra_patches=(),
        async_user=None,
        launch_params=None,
    ) -> SimpleNamespace:
        """
        Run ``pipeline`` for ``project_version``; the analyzer run reports ``launch_data`` over the defaults.

        The launch carries ``launch_params`` over a semgrep/python launch of ``project_version``. The report upload
        returns ``upload_results`` unless it is None, in which case the real import runs.
        Returns the analyzer-run and upload mocks as ``execute`` and ``upload``.
        """
        with ExitStack() as stack:
            stack.enter_context(patch("aist.models.AISTProjectVersion.ensure_extracted", return_value=None))
            stack.enter_context(
                patch(
                    "aist.tasks.sast_execution.install_pipeline_logging",
                    return_value=logging.getLogger("aist.test.sast_worker"),
                ),
            )
            stack.enter_context(patch("aist.tasks.sast_execution.AnalyzersConfigHelper"))
            execute = stack.enter_context(
                patch(
                    "aist.tasks.sast_execution.execute_pipeline",
                    return_value=SimpleNamespace(launch_data={**ANALYZER_RUN_LAUNCH_DATA, **(launch_data or {})}),
                ),
            )
            upload = (
                stack.enter_context(
                    patch("aist.tasks.sast_execution.upload_results_internal", return_value=list(upload_results)),
                )
                if upload_results is not None
                else None
            )
            for extra in extra_patches:
                stack.enter_context(extra)
            run_persisted_sast_pipeline(
                pipeline,
                {
                    "project_id": self.project.id,
                    "project_version": project_version.id,
                    "analyzers": ["semgrep"],
                    "selected_languages": ["python"],
                    **(launch_params or {}),
                },
                async_user=async_user,
            )
        return SimpleNamespace(execute=execute, upload=upload)

    def make_test_with_finding(self, *, file_path: str = "src/app.py") -> tuple[Test, Finding]:
        """Create a Test of this project's product holding one High finding at ``file_path``."""
        return make_test_with_finding(self.product, self.user, file_path=file_path)


def make_test_with_finding(
    product,
    reporter,
    *,
    file_path: str = "src/app.py",
    line: int | None = None,
    test_type_name: str = "SAST worker scan",
) -> tuple[Test, Finding]:
    """Create an engagement and a Test of ``product`` holding one High finding at ``file_path``."""
    engagement = Engagement.objects.create(
        name="SAST worker engagement",
        target_start=timezone.now(),
        target_end=timezone.now(),
        product=product,
    )
    dd_test = Test.objects.create(
        engagement=engagement,
        target_start=timezone.now(),
        target_end=timezone.now(),
        test_type=Test_Type.objects.get_or_create(name=test_type_name)[0],
    )
    finding = Finding.objects.create(
        test=dd_test,
        title="SAST worker finding",
        severity="High",
        date=timezone.now(),
        reporter=reporter,
        file_path=file_path,
        line=line,
    )
    return dd_test, finding
