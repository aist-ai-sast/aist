from __future__ import annotations

from contextlib import contextmanager
from io import StringIO
from types import SimpleNamespace
from unittest.mock import patch

from django.core.management import call_command
from django.utils import timezone
from dojo.models import DojoMeta, Engagement, Finding, Test, Test_Type

from aist.models import AISTPipeline, AISTStatus
from aist.tasks.pipeline import run_sast_pipeline
from aist.test.test_api import AISTApiBase
from aist.utils.pipeline import finish_pipeline
from aist.utils.reconciliation import reconcile_pipeline_orphans, safe_attach_findings_to_version


class _DummyLogger:
    def info(self, *args, **kwargs):
        return None

    def exception(self, *args, **kwargs):
        return None


@contextmanager
def _dummy_script_path_context():
    yield "aist-test-script.sh"


class PipelineOrphanReconciliationTests(AISTApiBase):
    def _make_test_with_finding(self, *, file_path: str = "src/app.py"):
        engagement = Engagement.objects.create(
            name="Reconcile Engage",
            target_start=timezone.now(),
            target_end=timezone.now(),
            product=self.product,
        )
        test_type = Test_Type.objects.create(name="Semgrep Reconcile")
        dd_test = Test.objects.create(
            engagement=engagement,
            target_start=timezone.now(),
            target_end=timezone.now(),
            test_type=test_type,
        )
        finding = Finding.objects.create(
            test=dd_test,
            title="Reconcile finding",
            severity="High",
            date=timezone.now(),
            reporter=self.user,
            file_path=file_path,
        )
        return dd_test, finding

    def _pipeline_params(self, pv_id: int) -> SimpleNamespace:
        descriptor = {
            "id": self.pv.id,
            "type": self.pv.version_type,
            "excluded_paths": [],
        }
        return SimpleNamespace(
            project_version={"id": pv_id, "version": "main"},
            project_name="test_product",
            languages=["python"],
            output_dir="/aist-output",
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

    def test_safe_attach_ignores_non_existing_findings(self):
        _dd_test, finding = self._make_test_with_finding(file_path="src/attach-ignore.py")
        missing_id = finding.id + 999999

        stats = safe_attach_findings_to_version(
            pv=self.pv,
            finding_ids=[finding.id, missing_id],
            logger=_DummyLogger(),
        )

        self.assertEqual(stats.requested, 2)
        self.assertEqual(stats.missing_before_insert, 1)
        self.assertTrue(self.pv.findings.filter(id=finding.id).exists())
        self.assertFalse(self.pv.findings.filter(id=missing_id).exists())

    def test_pipeline_keeps_tests_and_finishes_with_warnings_on_postprocess_failure(self):
        dd_test, finding = self._make_test_with_finding()
        pipeline = AISTPipeline.objects.create(
            id="pipe-orphan-int-1",
            project=self.project,
            project_version=self.pv,
            status=AISTStatus.FINISHED,
        )
        with (
            patch("aist.tasks.pipeline.PipelineArguments.from_dict", return_value=self._pipeline_params(self.pv.id)),
            patch("aist.tasks.pipeline.AISTProjectVersion.ensure_extracted", return_value=None),
            patch("aist.tasks.pipeline.get_project_build_path", return_value="/aist-project"),
            patch("aist.tasks.pipeline.install_pipeline_logging", return_value=_DummyLogger()),
            patch("aist.tasks.pipeline.AnalyzersConfigHelper"),
            patch(
                "aist.tasks.pipeline.configure_project_run_analyses",
                return_value={
                    "git": {},
                    "output_dir": "/aist-output",
                    "project_path": "/aist-project",
                    "trim_path": "",
                    "tmp_analyzer_config_path": "/aist-analyzers.yml",
                },
            ),
            patch("aist.tasks.pipeline.upload_results_internal", return_value=[SimpleNamespace(test_id=dd_test.id)]),
            patch("aist.tasks.pipeline.postprocess_findings", side_effect=RuntimeError("forced crash")),
            self.assertRaises(RuntimeError),
        ):
            run_sast_pipeline.run(pipeline.id, {"project_id": self.project.id})

        pipeline.refresh_from_db()
        self.assertTrue(pipeline.tests.filter(id=dd_test.id).exists())
        self.assertEqual(pipeline.status, AISTStatus.FINISHED_WITH_WARNINGS)
        self.assertEqual((pipeline.launch_data or {}).get("imported_test_ids"), [dd_test.id])
        self.assertTrue(self.pv.findings.filter(id=finding.id).exists())

    def test_finish_pipeline_reconciles_orphans_to_finished(self):
        dd_test, finding = self._make_test_with_finding(file_path="src/recovered.py")
        pipeline = AISTPipeline.objects.create(
            id="pipe-orphan-fix-1",
            project=self.project,
            project_version=self.pv,
            status=AISTStatus.WAITING_DEDUPLICATION_TO_FINISH,
            launch_data={"imported_test_ids": [dd_test.id]},
        )

        finish_pipeline(pipeline)
        pipeline.refresh_from_db()

        self.assertEqual(pipeline.status, AISTStatus.FINISHED)
        self.assertTrue(pipeline.tests.filter(id=dd_test.id).exists())
        self.assertTrue(self.pv.findings.filter(id=finding.id).exists())
        self.assertTrue(
            DojoMeta.objects.filter(
                finding=finding,
                name="sourcefile_link",
            ).exists(),
        )

    def test_reconciliation_skips_source_link_for_excluded_path_without_deletion(self):
        self.project.profile = {"paths": {"exclude": ["vendor/"]}}
        self.project.save(update_fields=["profile"])
        dd_test, finding = self._make_test_with_finding(file_path="vendor/lib/unsafe.py")
        pipeline = AISTPipeline.objects.create(
            id="pipe-orphan-excluded-1",
            project=self.project,
            project_version=self.pv,
            status=AISTStatus.WAITING_DEDUPLICATION_TO_FINISH,
            launch_data={"imported_test_ids": [dd_test.id]},
        )

        stats = reconcile_pipeline_orphans(pipeline_id=pipeline.id, dry_run=False)
        finding.refresh_from_db()

        self.assertTrue(Finding.objects.filter(id=finding.id).exists())
        self.assertFalse(
            DojoMeta.objects.filter(
                finding=finding,
                name="sourcefile_link",
            ).exists(),
        )
        self.assertEqual(stats["remaining_sourcefile_link_violations"], 0)

    def test_reconcile_command_dry_run_and_apply(self):
        dd_test, finding = self._make_test_with_finding(file_path="src/command.py")
        pipeline = AISTPipeline.objects.create(
            id="pipe-orphan-cmd-1",
            project=self.project,
            project_version=self.pv,
            status=AISTStatus.WAITING_DEDUPLICATION_TO_FINISH,
            launch_data={"imported_test_ids": [dd_test.id]},
        )

        dry_stdout = StringIO()
        call_command(
            "reconcile_aist_orphans",
            "--hours",
            "24",
            "--batch-size",
            "100",
            "--dry-run",
            stdout=dry_stdout,
        )
        pipeline.refresh_from_db()
        self.assertFalse(pipeline.tests.filter(id=dd_test.id).exists())
        self.assertFalse(self.pv.findings.filter(id=finding.id).exists())

        run_stdout = StringIO()
        call_command(
            "reconcile_aist_orphans",
            "--hours",
            "24",
            "--batch-size",
            "100",
            stdout=run_stdout,
        )
        pipeline.refresh_from_db()
        self.assertTrue(pipeline.tests.filter(id=dd_test.id).exists())
        self.assertTrue(self.pv.findings.filter(id=finding.id).exists())
