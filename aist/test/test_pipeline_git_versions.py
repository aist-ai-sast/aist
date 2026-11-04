from __future__ import annotations

import logging
from types import SimpleNamespace
from unittest.mock import patch

from dojo.models import Finding

from aist.internal_upload import ImportResult
from aist.models import AISTPipeline, AISTProjectVersion, VersionType
from aist.test.pipeline_execution_helpers import SastWorkerTestMixin, run_persisted_sast_pipeline
from aist.test.test_api import AISTApiBase

RESOLVED_COMMIT = "1234567890abcdef1234567890abcdef12345678"


class PipelineGitVersionResolutionTests(SastWorkerTestMixin, AISTApiBase):
    def _branch_pipeline(self, pipeline_id: str) -> tuple[AISTPipeline, AISTProjectVersion]:
        branch = AISTProjectVersion.objects.create(
            project=self.project,
            version_type=VersionType.GIT_BRANCH,
            version="main",
        )
        pipeline = AISTPipeline.objects.create(
            id=pipeline_id,
            project=self.project,
            project_version=branch,
            status="FINISHED",
        )
        return pipeline, branch

    def test_launch_fails_when_pipeline_and_params_project_version_mismatch(self):
        version_a = AISTProjectVersion.objects.create(
            project=self.project,
            version_type=VersionType.GIT_BRANCH,
            version="main",
        )
        version_b = AISTProjectVersion.objects.create(
            project=self.project,
            version_type=VersionType.GIT_BRANCH,
            version="develop",
        )
        pipeline = AISTPipeline.objects.create(
            id="pipe-mismatch-1",
            project=self.project,
            project_version=version_a,
            status="FINISHED",
        )

        with (
            patch("aist.tasks.sast_execution.install_pipeline_logging", return_value=logging.getLogger(__name__)),
            patch("aist.tasks.sast_execution.execute_pipeline") as execute,
            self.assertRaises(ValueError),
        ):
            run_persisted_sast_pipeline(pipeline, {
                "project_id": self.project.id,
                "project_version": version_b.id,
                "analyzers": ["semgrep"],
                "selected_languages": ["python"],
            })

        execute.assert_not_called()

    def test_branch_launch_creates_git_hash_and_relinks_pipeline(self):
        pipeline, branch = self._branch_pipeline("pipe-branch-1")

        self.run_sast(
            pipeline,
            project_version=branch,
            launch_data={"git": {"resolved_commit": RESOLVED_COMMIT}},
            upload_results=[],
        )

        pipeline.refresh_from_db()
        branch.refresh_from_db()
        self.assertEqual(pipeline.project_version.version_type, VersionType.GIT_HASH)
        self.assertEqual(pipeline.project_version.version, RESOLVED_COMMIT)
        self.assertEqual(pipeline.project_version.resolved_from_branch_id, branch.id)
        self.assertEqual(branch.last_resolved_commit, RESOLVED_COMMIT)

    def test_branch_launch_attaches_findings_to_hash_and_branch_versions(self):
        pipeline, branch = self._branch_pipeline("pipe-branch-2")
        dd_test, finding = self.make_test_with_finding()

        with patch("aist.services.pipeline_results.schedule_pipeline_postprocessing") as mock_postprocess:
            self.run_sast(
                pipeline,
                project_version=branch,
                launch_data={"git": {"resolved_commit": RESOLVED_COMMIT}},
                upload_results=[ImportResult(analyzer_name="semgrep", test_id=dd_test.id)],
            )

        pipeline.refresh_from_db()
        hash_version = pipeline.project_version
        self.assertEqual(hash_version.version_type, VersionType.GIT_HASH)
        self.assertEqual(hash_version.version, RESOLVED_COMMIT)
        self.assertTrue(hash_version.findings.filter(id=finding.id).exists())
        self.assertTrue(branch.findings.filter(id=finding.id).exists())
        mock_postprocess.assert_called_once_with(pipeline.id, "INFO")

    def test_branch_launch_ignores_missing_finding_ids_before_m2m_and_postprocess(self):
        pipeline, branch = self._branch_pipeline("pipe-branch-3")
        dd_test, finding = self.make_test_with_finding()
        missing_finding_id = finding.id + 100000
        original_finding_filter = Finding.objects.filter

        def _finding_filter(*args, **kwargs):
            if "test_id__in" in kwargs:
                return SimpleNamespace(values_list=lambda *_args, **_kwargs: [finding.id, missing_finding_id])
            return original_finding_filter(*args, **kwargs)

        with patch("aist.services.pipeline_results.schedule_pipeline_postprocessing") as mock_postprocess:
            self.run_sast(
                pipeline,
                project_version=branch,
                launch_data={"git": {"resolved_commit": RESOLVED_COMMIT}},
                upload_results=[ImportResult(analyzer_name="semgrep", test_id=dd_test.id)],
                extra_patches=[patch("aist.tasks.sast_execution.Finding.objects.filter", side_effect=_finding_filter)],
            )

        pipeline.refresh_from_db()
        branch.refresh_from_db()
        hash_version = pipeline.project_version
        self.assertEqual(hash_version.version_type, VersionType.GIT_HASH)
        self.assertTrue(hash_version.findings.filter(id=finding.id).exists())
        self.assertFalse(hash_version.findings.filter(id=missing_finding_id).exists())
        self.assertTrue(branch.findings.filter(id=finding.id).exists())
        self.assertFalse(branch.findings.filter(id=missing_finding_id).exists())
        mock_postprocess.assert_called_once_with(pipeline.id, "INFO")
