from __future__ import annotations

import logging
import tempfile
from datetime import timedelta
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from django.core.management import call_command
from django.utils import timezone
from dojo.models import DojoMeta

from aist.execution.dispatching import LaunchAcceptance
from aist.internal_upload import ImportResult
from aist.link_builder import SOURCEFILE_LINK_META_NAME, LinkBuilder
from aist.models import (
    AISTPipeline,
    AISTStatus,
    PipelineExecutionType,
    PipelineLaunchRequest,
    PipelineLaunchRequestState,
)
from aist.services.pipeline_results import schedule_pipeline_postprocessing
from aist.tasks.pipeline import run_pipeline_execution
from aist.tasks.reconciliation import (
    _recover_stuck_dedup_pipelines,
    _recover_stuck_enrich_pipelines,
    reconcile_recent_orphans_task,
)
from aist.test.pipeline_execution_helpers import SastWorkerTestMixin, make_test_with_finding
from aist.test.test_api import AISTApiBase
from aist.utils.pipeline import finish_pipeline
from aist.utils.reconciliation import reconcile_pipeline_orphans, safe_attach_findings_to_version

_LOGGER = logging.getLogger(__name__)


class PipelineOrphanReconciliationTests(SastWorkerTestMixin, AISTApiBase):
    def test_safe_attach_skips_findings_deleted_before_lock(self):
        """
        safe_attach_findings_to_version must not raise when a finding is deleted
        between the existence-check SELECT and the FOR KEY SHARE lock (i.e. it was
        gone by the time we tried to lock it).  The deleted finding is simply skipped
        and does not cause a FK violation at COMMIT time.
        """
        _dd_test, finding = self.make_test_with_finding(file_path="src/deleted-before-lock.py")
        deleted_id = finding.id
        finding.delete()

        stats = safe_attach_findings_to_version(
            pv=self.pv,
            finding_ids=[deleted_id],
            logger=_LOGGER,
        )

        self.assertEqual(stats.requested, 1)
        self.assertEqual(stats.missing_before_insert, 1)
        self.assertEqual(stats.linked, 0)
        self.assertFalse(self.pv.findings.filter(id=deleted_id).exists())

    def test_safe_attach_ignores_non_existing_findings(self):
        _dd_test, finding = self.make_test_with_finding(file_path="src/attach-ignore.py")
        missing_id = finding.id + 999999

        stats = safe_attach_findings_to_version(
            pv=self.pv,
            finding_ids=[finding.id, missing_id],
            logger=_LOGGER,
        )

        self.assertEqual(stats.requested, 2)
        self.assertEqual(stats.missing_before_insert, 1)
        self.assertTrue(self.pv.findings.filter(id=finding.id).exists())
        self.assertFalse(self.pv.findings.filter(id=missing_id).exists())

    def test_pipeline_keeps_tests_and_finishes_with_warnings_on_postprocess_failure(self):
        dd_test, finding = self.make_test_with_finding()
        pipeline = AISTPipeline.objects.create(
            id="pipe-orphan-int-1",
            project=self.project,
            project_version=self.pv,
            status=AISTStatus.FINISHED,
        )
        with self.assertRaises(RuntimeError):
            self.run_sast(
                pipeline,
                project_version=self.pv,
                upload_results=[ImportResult(analyzer_name="semgrep", test_id=dd_test.id)],
                extra_patches=[
                    patch(
                        "aist.services.pipeline_results.schedule_pipeline_postprocessing",
                        side_effect=RuntimeError("forced crash"),
                    ),
                ],
            )

        pipeline.refresh_from_db()
        self.assertTrue(pipeline.tests.filter(id=dd_test.id).exists())
        self.assertEqual(pipeline.status, AISTStatus.FINISHED_WITH_WARNINGS)
        self.assertEqual((pipeline.launch_data or {}).get("imported_test_ids"), [dd_test.id])
        self.assertTrue(self.pv.findings.filter(id=finding.id).exists())

    def test_duplicate_start_does_not_delete_current_workspace(self):
        pipeline = AISTPipeline.objects.create(
            id="pipe-duplicate-workspace-1",
            project=self.project,
            project_version=self.pv,
            status=AISTStatus.EXECUTING,
        )
        with tempfile.TemporaryDirectory() as base:
            run_dir = Path(base) / "test_product" / "main" / "runs" / pipeline.id
            run_dir.mkdir(parents=True)
            (run_dir / "keep.txt").write_text("data")

            with (
                self.settings(AIST_PROJECTS_BUILD_DIR=base),
                patch("aist.tasks.sast_execution.install_pipeline_logging", return_value=_LOGGER),
                patch(
                    "aist.tasks.pipeline.accept_published_launch",
                    return_value=LaunchAcceptance.DUPLICATE,
                ),
            ):
                run_pipeline_execution.run(pipeline.id)

            self.assertTrue((run_dir / "keep.txt").exists())

    def test_finish_pipeline_reconciles_orphans_to_finished(self):
        dd_test, finding = self.make_test_with_finding(file_path="src/recovered.py")
        pipeline = AISTPipeline.objects.create(
            id="pipe-orphan-fix-1",
            project=self.project,
            project_version=self.pv,
            status=AISTStatus.WAITING_DEDUPLICATION_TO_FINISH,
            launch_data={"imported_test_ids": [dd_test.id]},
        )

        finish_pipeline(pipeline.id)
        pipeline.refresh_from_db()

        self.assertEqual(pipeline.status, AISTStatus.FINISHED)
        self.assertTrue(pipeline.tests.filter(id=dd_test.id).exists())
        self.assertTrue(self.pv.findings.filter(id=finding.id).exists())
        self.assertTrue(DojoMeta.objects.filter(finding=finding, name=SOURCEFILE_LINK_META_NAME).exists())

    def test_reconcile_command_dry_run_and_apply(self):
        dd_test, finding = self.make_test_with_finding(file_path="src/command.py")
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


class PostprocessFindingsTests(AISTApiBase):
    def test_watch_dedup_task_id_saved_atomically_with_status(self):
        """
        watch_dedup_task_id must be visible in the DB at the same time as the
        WAITING_DEDUPLICATION_TO_FINISH status — no window where status is set
        but task_id is still None.
        """
        pipeline = AISTPipeline.objects.create(
            id="pipe-postproc-atomic-1",
            project=self.project,
            project_version=self.pv,
            status=AISTStatus.UPLOADING_RESULTS,
        )

        with (
            patch("aist.services.pipeline_results.current_app") as mock_app,
            self.captureOnCommitCallbacks(execute=True),
        ):
            schedule_pipeline_postprocessing(pipeline.id, "INFO")

        pipeline.refresh_from_db()
        self.assertEqual(pipeline.status, AISTStatus.WAITING_DEDUPLICATION_TO_FINISH)
        self.assertIsNotNone(pipeline.watch_dedup_task_id)

        # The task must have been dispatched with the same ID that was saved
        apply_async = mock_app.signature.return_value.apply_async
        apply_async.assert_called_once()
        call_kwargs = apply_async.call_args
        dispatched_task_id = call_kwargs.kwargs.get("task_id") or (
            call_kwargs[1].get("task_id") if len(call_kwargs) > 1 else None
        )
        self.assertEqual(dispatched_task_id, pipeline.watch_dedup_task_id)


class ReconciliationRecoveryTests(AISTApiBase):
    def test_full_reconciliation_dry_run_does_not_requeue_stale_launch_request(self):
        stale_request = PipelineLaunchRequest.objects.create(
            project=self.project,
            state=PipelineLaunchRequestState.CLAIMED,
            claim_owner="dry-run-owner",
            claimed_at=timezone.now() - timedelta(hours=1),
        )

        result = reconcile_recent_orphans_task.run(dry_run=True)

        stale_request.refresh_from_db()
        self.assertEqual(stale_request.state, PipelineLaunchRequestState.CLAIMED)
        self.assertEqual(result["launch_requests"], {"dry_run": True})

    def test_recover_stuck_pipeline_helpers_dry_run_do_not_mutate_pipelines(self):
        dedup_pipeline = AISTPipeline.objects.create(
            id="pipe-reconcile-dry-dedup-1",
            project=self.project,
            project_version=self.pv,
            status=AISTStatus.WAITING_DEDUPLICATION_TO_FINISH,
        )
        enrich_pipeline = AISTPipeline.objects.create(
            id="pipe-reconcile-dry-enrich-1",
            project=self.project,
            project_version=self.pv,
            status=AISTStatus.FINDING_POSTPROCESSING,
        )
        old_updated = timezone.now() - timedelta(hours=1)
        AISTPipeline.objects.filter(id=dedup_pipeline.id).update(updated=old_updated)
        AISTPipeline.objects.filter(id=enrich_pipeline.id).update(updated=old_updated)

        dedup_recovered = _recover_stuck_dedup_pipelines(dry_run=True)
        enrich_recovered = _recover_stuck_enrich_pipelines(dry_run=True)

        dedup_pipeline.refresh_from_db()
        enrich_pipeline.refresh_from_db()

        self.assertEqual(dedup_recovered, 1)
        self.assertEqual(enrich_recovered, 1)
        self.assertEqual(dedup_pipeline.status, AISTStatus.WAITING_DEDUPLICATION_TO_FINISH)
        self.assertIsNone(dedup_pipeline.watch_dedup_task_id)
        self.assertEqual(enrich_pipeline.status, AISTStatus.FINDING_POSTPROCESSING)


class PipelineFinishStatusTests(AISTApiBase):

    """How a pipeline ends once its results are in: reconciliation failures and leftovers end it with warnings."""

    def _uploading_pipeline(self, pipeline_id: str, *, project_version, execution_type=PipelineExecutionType.SAST):
        return AISTPipeline.objects.create(
            id=pipeline_id,
            project=self.project,
            project_version=project_version,
            execution_type=execution_type,
            status=AISTStatus.UPLOADING_RESULTS,
        )

    def test_a_clean_pipeline_finishes(self):
        pipeline = self._uploading_pipeline("pipe-finish-clean", project_version=self.pv)

        finish_pipeline(pipeline.id)

        pipeline.refresh_from_db()
        self.assertEqual(pipeline.status, AISTStatus.FINISHED)

    def test_a_versionless_dast_pipeline_with_findings_finishes_with_warnings_and_links_no_source(self):
        pipeline = self._uploading_pipeline(
            "pipe-finish-versionless", project_version=None, execution_type=PipelineExecutionType.DAST,
        )
        dd_test, finding = make_test_with_finding(self.product, self.user, file_path="src/app.py")
        pipeline.tests.add(dd_test)

        with self.assertNoLogs("aist", level="ERROR"):
            stats = reconcile_pipeline_orphans(pipeline_id=pipeline.id, dry_run=True)
            finish_pipeline(pipeline.id)

        self.assertEqual(stats.source_links.eligible, 0)
        self.assertEqual(stats.remaining_missing_project_version_links, 1)
        pipeline.refresh_from_db()
        self.assertEqual(pipeline.status, AISTStatus.FINISHED_WITH_WARNINGS)
        self.assertFalse(DojoMeta.objects.filter(finding=finding, name=SOURCEFILE_LINK_META_NAME).exists())

    def test_a_finding_left_with_its_scanned_path_is_linked_relative_to_the_checkout(self):
        pipeline = self._uploading_pipeline("pipe-finish-untrimmed", project_version=self.pv)
        pipeline.launch_data = {"trim_path": "/aist-build/widgets"}
        pipeline.save(update_fields=["launch_data"])
        dd_test, finding = make_test_with_finding(
            self.product, self.user, file_path="/aist-build/widgets/src/app.py",
        )
        pipeline.tests.add(dd_test)

        finish_pipeline(pipeline.id)

        link = DojoMeta.objects.get(finding=finding, name=SOURCEFILE_LINK_META_NAME).value
        self.assertEqual(LinkBuilder.parse(link), (self.pv.id, "src/app.py"))

    def test_a_reconciliation_failure_finishes_the_pipeline_with_warnings(self):
        pipeline = self._uploading_pipeline("pipe-finish-reconcile-crash", project_version=self.pv)

        with patch("aist.utils.pipeline.reconcile_pipeline_orphans", side_effect=RuntimeError("reconcile crashed")):
            finish_pipeline(pipeline.id)

        pipeline.refresh_from_db()
        self.assertEqual(pipeline.status, AISTStatus.FINISHED_WITH_WARNINGS)
