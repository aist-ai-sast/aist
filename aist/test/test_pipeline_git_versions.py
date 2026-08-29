from __future__ import annotations

import tempfile
from types import SimpleNamespace
from unittest.mock import patch

from django.utils import timezone
from dojo.models import Engagement, Finding, Test, Test_Type

from aist.execution.dispatching import LaunchAcceptance
from aist.models import AISTPipeline, AISTProjectVersion, VersionType
from aist.test.pipeline_execution_helpers import run_persisted_sast_pipeline
from aist.test.test_api import AISTApiBase


class _DummyLogger:
    def info(self, *args, **kwargs):
        return None

    def exception(self, *args, **kwargs):
        return None


class PipelineGitVersionResolutionTests(AISTApiBase):
    def setUp(self):
        super().setUp()
        acceptance = patch(
            "aist.tasks.pipeline.accept_published_launch",
            return_value=LaunchAcceptance.ACCEPTED,
        )
        acceptance.start()
        self.addCleanup(acceptance.stop)
        self._runtime_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._runtime_dir.cleanup)
        settings_override = self.settings(
            AIST_PROJECTS_BUILD_DIR=f"{self._runtime_dir.name}/build",
            AIST_OUTPUT_PATH=f"{self._runtime_dir.name}/output",
        )
        settings_override.enable()
        self.addCleanup(settings_override.disable)

    def _params(self, project_version):
        return {
            "project_id": self.project.id,
            "project_version": project_version.id,
            "analyzers": ["semgrep"],
            "selected_languages": ["python"],
        }

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
            patch("aist.tasks.pipeline.AISTProjectVersion.ensure_extracted", return_value=None),
            patch("aist.tasks.pipeline.install_pipeline_logging", return_value=_DummyLogger()),
            patch("aist.tasks.pipeline.AnalyzersConfigHelper"),
            patch("aist.tasks.pipeline.execute_pipeline") as mock_configure,
            patch("aist.tasks.pipeline.upload_results_internal", return_value=[]),
        ):
            with self.assertRaises(ValueError):
                run_persisted_sast_pipeline(pipeline, self._params(version_b))

            mock_configure.assert_not_called()

    def test_branch_launch_creates_git_hash_and_relinks_pipeline(self):
        branch = AISTProjectVersion.objects.create(
            project=self.project,
            version_type=VersionType.GIT_BRANCH,
            version="main",
        )
        pipeline = AISTPipeline.objects.create(
            id="pipe-branch-1",
            project=self.project,
            project_version=branch,
            status="FINISHED",
        )

        resolved_commit = "1234567890abcdef1234567890abcdef12345678"
        with (
            patch("aist.tasks.pipeline.AISTProjectVersion.ensure_extracted", return_value=None),
            patch("aist.tasks.pipeline.install_pipeline_logging", return_value=_DummyLogger()),
            patch("aist.tasks.pipeline.AnalyzersConfigHelper"),
            patch("aist.tasks.pipeline.execute_pipeline") as mock_configure,
            patch("aist.tasks.pipeline.upload_results_internal", return_value=[]),
        ):
            mock_configure.return_value = SimpleNamespace(launch_data={
                "git": {"resolved_commit": resolved_commit},
                "output_dir": "/aist-output",
                "project_path": "/aist-project",
                "trim_path": "",
                "tmp_analyzer_config_path": "/aist-analyzers.yml",
            })

            run_persisted_sast_pipeline(pipeline, self._params(branch))

        pipeline.refresh_from_db()
        branch.refresh_from_db()

        self.assertEqual(pipeline.project_version.version_type, VersionType.GIT_HASH)
        self.assertEqual(pipeline.project_version.version, resolved_commit)
        self.assertEqual(pipeline.project_version.resolved_from_branch_id, branch.id)
        self.assertEqual(branch.last_resolved_commit, resolved_commit)

    def test_branch_launch_attaches_findings_to_hash_and_branch_versions(self):
        branch = AISTProjectVersion.objects.create(
            project=self.project,
            version_type=VersionType.GIT_BRANCH,
            version="main",
        )
        pipeline = AISTPipeline.objects.create(
            id="pipe-branch-2",
            project=self.project,
            project_version=branch,
            status="FINISHED",
        )
        engagement = Engagement.objects.create(
            name="Engage",
            target_start=timezone.now(),
            target_end=timezone.now(),
            product=self.product,
        )
        test_type = Test_Type.objects.create(name="Semgrep")
        dd_test = Test.objects.create(
            engagement=engagement,
            target_start=timezone.now(),
            target_end=timezone.now(),
            test_type=test_type,
        )
        finding = Finding.objects.create(
            test=dd_test,
            title="Finding A",
            severity="High",
            date=timezone.now(),
            reporter=self.user,
        )

        resolved_commit = "1234567890abcdef1234567890abcdef12345678"
        with (
            patch("aist.tasks.pipeline.AISTProjectVersion.ensure_extracted", return_value=None),
            patch("aist.tasks.pipeline.install_pipeline_logging", return_value=_DummyLogger()),
            patch("aist.tasks.pipeline.AnalyzersConfigHelper"),
            patch("aist.tasks.pipeline.execute_pipeline") as mock_configure,
            patch("aist.tasks.pipeline.upload_results_internal", return_value=[SimpleNamespace(test_id=dd_test.id)]),
            patch("aist.tasks.pipeline.postprocess_findings", return_value=SimpleNamespace()) as mock_postprocess,
        ):
            mock_configure.return_value = SimpleNamespace(launch_data={
                "git": {"resolved_commit": resolved_commit},
                "output_dir": "/aist-output",
                "project_path": "/aist-project",
                "trim_path": "",
                "tmp_analyzer_config_path": "/aist-analyzers.yml",
            })

            run_persisted_sast_pipeline(pipeline, self._params(branch))

        pipeline.refresh_from_db()
        branch.refresh_from_db()

        hash_version = pipeline.project_version
        self.assertEqual(hash_version.version_type, VersionType.GIT_HASH)
        self.assertEqual(hash_version.version, resolved_commit)
        self.assertTrue(hash_version.findings.filter(id=finding.id).exists())
        self.assertTrue(branch.findings.filter(id=finding.id).exists())
        mock_postprocess.assert_called_once_with(pipeline.id, "INFO")

    def test_branch_launch_ignores_missing_finding_ids_before_m2m_and_postprocess(self):
        branch = AISTProjectVersion.objects.create(
            project=self.project,
            version_type=VersionType.GIT_BRANCH,
            version="main",
        )
        pipeline = AISTPipeline.objects.create(
            id="pipe-branch-3",
            project=self.project,
            project_version=branch,
            status="FINISHED",
        )
        engagement = Engagement.objects.create(
            name="Engage",
            target_start=timezone.now(),
            target_end=timezone.now(),
            product=self.product,
        )
        test_type = Test_Type.objects.create(name="Semgrep")
        dd_test = Test.objects.create(
            engagement=engagement,
            target_start=timezone.now(),
            target_end=timezone.now(),
            test_type=test_type,
        )
        finding = Finding.objects.create(
            test=dd_test,
            title="Finding A",
            severity="High",
            date=timezone.now(),
            reporter=self.user,
        )

        missing_finding_id = finding.id + 100000
        resolved_commit = "1234567890abcdef1234567890abcdef12345678"
        original_finding_filter = Finding.objects.filter

        with (
            patch("aist.tasks.pipeline.AISTProjectVersion.ensure_extracted", return_value=None),
            patch("aist.tasks.pipeline.install_pipeline_logging", return_value=_DummyLogger()),
            patch("aist.tasks.pipeline.AnalyzersConfigHelper"),
            patch("aist.tasks.pipeline.execute_pipeline") as mock_configure,
            patch("aist.tasks.pipeline.upload_results_internal", return_value=[SimpleNamespace(test_id=dd_test.id)]),
            patch("aist.tasks.pipeline.postprocess_findings", return_value=SimpleNamespace()) as mock_postprocess,
            patch("aist.tasks.pipeline.Finding.objects.filter") as mock_finding_filter,
        ):
            def _finding_filter(*args, **kwargs):
                if "test_id__in" in kwargs:
                    return SimpleNamespace(values_list=lambda *_args, **_kwargs: [finding.id, missing_finding_id])
                return original_finding_filter(*args, **kwargs)

            mock_finding_filter.side_effect = _finding_filter
            mock_configure.return_value = SimpleNamespace(launch_data={
                "git": {"resolved_commit": resolved_commit},
                "output_dir": "/aist-output",
                "project_path": "/aist-project",
                "trim_path": "",
                "tmp_analyzer_config_path": "/aist-analyzers.yml",
            })

            run_persisted_sast_pipeline(pipeline, self._params(branch))

        pipeline.refresh_from_db()
        branch.refresh_from_db()

        hash_version = pipeline.project_version
        self.assertEqual(hash_version.version_type, VersionType.GIT_HASH)
        self.assertTrue(hash_version.findings.filter(id=finding.id).exists())
        self.assertFalse(hash_version.findings.filter(id=missing_finding_id).exists())
        self.assertTrue(branch.findings.filter(id=finding.id).exists())
        self.assertFalse(branch.findings.filter(id=missing_finding_id).exists())
        mock_postprocess.assert_called_once_with(pipeline.id, "INFO")
