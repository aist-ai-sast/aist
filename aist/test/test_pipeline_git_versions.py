from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import patch

from django.utils import timezone
from dojo.models import Engagement, Finding, Test, Test_Type

from aist.execution.dispatching import LaunchAcceptance
from aist.models import AISTPipeline, AISTProjectVersion, VersionType
from aist.tasks.pipeline import run_sast_pipeline
from aist.test.test_api import AISTApiBase


class _DummyLogger:
    def info(self, *args, **kwargs):
        return None

    def exception(self, *args, **kwargs):
        return None


@contextmanager
def _dummy_script_path_context():
    yield "aist-test-script.sh"


class PipelineGitVersionResolutionTests(AISTApiBase):
    def setUp(self):
        super().setUp()
        acceptance = patch(
            "aist.tasks.pipeline.accept_published_launch",
            return_value=LaunchAcceptance.ACCEPTED,
        )
        acceptance.start()
        self.addCleanup(acceptance.stop)

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
            patch("aist.tasks.pipeline.PipelineArguments.from_dict") as mock_from_dict,
            patch("aist.tasks.pipeline.AISTProjectVersion.ensure_extracted", return_value=None),
            patch("aist.tasks.pipeline.get_project_build_path", return_value="/aist-project"),
            patch("aist.tasks.pipeline.install_pipeline_logging", return_value=_DummyLogger()),
            patch("aist.tasks.pipeline.AnalyzersConfigHelper"),
            patch("aist.tasks.pipeline.configure_project_run_analyses") as mock_configure,
            patch("aist.tasks.pipeline.upload_results_internal", return_value=[]),
        ):
            mock_from_dict.return_value = SimpleNamespace(
                project_version={"id": version_b.id, "version": "develop"},
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
                enrich_config=lambda: {
                    "project_version_descriptor": {"id": version_b.id, "version": "develop"},
                    "log_level": "INFO",
                },
            )

            with self.assertRaises(ValueError):
                run_sast_pipeline.run(pipeline.id, {"project_id": self.project.id})

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
            patch("aist.tasks.pipeline.PipelineArguments.from_dict") as mock_from_dict,
            patch("aist.tasks.pipeline.AISTProjectVersion.ensure_extracted", return_value=None),
            patch("aist.tasks.pipeline.get_project_build_path", return_value="/aist-project"),
            patch("aist.tasks.pipeline.install_pipeline_logging", return_value=_DummyLogger()),
            patch("aist.tasks.pipeline.AnalyzersConfigHelper"),
            patch("aist.tasks.pipeline.configure_project_run_analyses") as mock_configure,
            patch("aist.tasks.pipeline.upload_results_internal", return_value=[]),
        ):
            project_version_state = {"id": branch.id, "version": "main", "type": VersionType.GIT_BRANCH}

            def _resolve_effective_project_version(*, resolved_commit=""):
                commit = (resolved_commit or "").strip()
                pv_id = project_version_state.get("id")
                current_project_version = AISTProjectVersion.objects.filter(pk=pv_id).first() if pv_id else None
                if current_project_version and commit and current_project_version.version_type == VersionType.GIT_BRANCH:
                    resolved, _ = AISTProjectVersion.objects.get_or_create(
                        project_id=current_project_version.project_id,
                        version=commit,
                        version_type=VersionType.GIT_HASH,
                        defaults={"resolved_from_branch": current_project_version},
                    )
                    if resolved.resolved_from_branch_id is None:
                        resolved.resolved_from_branch = current_project_version
                        resolved.save(update_fields=["resolved_from_branch", "updated"])
                    current_project_version.last_resolved_commit = commit
                    current_project_version.save(update_fields=["last_resolved_commit", "updated"])
                    project_version_state.update(resolved.as_dict())
                    return resolved
                return current_project_version

            mock_from_dict.return_value = SimpleNamespace(
                project_version=project_version_state,
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
                resolve_effective_project_version=_resolve_effective_project_version,
                build_project_version_descriptor=lambda: {
                    **project_version_state,
                    "excluded_paths": [],
                },
                enrich_config=lambda: {
                    "project_version_descriptor": {
                        **project_version_state,
                        "excluded_paths": [],
                    },
                    "log_level": "INFO",
                },
            )
            mock_configure.return_value = {
                "git": {"resolved_commit": resolved_commit},
                "output_dir": "/aist-output",
                "project_path": "/aist-project",
                "trim_path": "",
                "tmp_analyzer_config_path": "/aist-analyzers.yml",
            }

            run_sast_pipeline.run(pipeline.id, {"project_id": self.project.id})

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
            patch("aist.tasks.pipeline.PipelineArguments.from_dict") as mock_from_dict,
            patch("aist.tasks.pipeline.AISTProjectVersion.ensure_extracted", return_value=None),
            patch("aist.tasks.pipeline.get_project_build_path", return_value="/aist-project"),
            patch("aist.tasks.pipeline.install_pipeline_logging", return_value=_DummyLogger()),
            patch("aist.tasks.pipeline.AnalyzersConfigHelper"),
            patch("aist.tasks.pipeline.configure_project_run_analyses") as mock_configure,
            patch("aist.tasks.pipeline.upload_results_internal", return_value=[SimpleNamespace(test_id=dd_test.id)]),
            patch("aist.tasks.pipeline.postprocess_findings", return_value=SimpleNamespace()) as mock_postprocess,
        ):
            project_version_state = {"id": branch.id, "version": "main", "type": VersionType.GIT_BRANCH}

            def _resolve_effective_project_version(*, resolved_commit=""):
                commit = (resolved_commit or "").strip()
                pv_id = project_version_state.get("id")
                current_project_version = AISTProjectVersion.objects.filter(pk=pv_id).first() if pv_id else None
                if current_project_version and commit and current_project_version.version_type == VersionType.GIT_BRANCH:
                    resolved, _ = AISTProjectVersion.objects.get_or_create(
                        project_id=current_project_version.project_id,
                        version=commit,
                        version_type=VersionType.GIT_HASH,
                        defaults={"resolved_from_branch": current_project_version},
                    )
                    if resolved.resolved_from_branch_id is None:
                        resolved.resolved_from_branch = current_project_version
                        resolved.save(update_fields=["resolved_from_branch", "updated"])
                    current_project_version.last_resolved_commit = commit
                    current_project_version.save(update_fields=["last_resolved_commit", "updated"])
                    project_version_state.update(resolved.as_dict())
                    return resolved
                return current_project_version

            mock_from_dict.return_value = SimpleNamespace(
                project_version=project_version_state,
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
                resolve_effective_project_version=_resolve_effective_project_version,
                build_project_version_descriptor=lambda: {
                    **project_version_state,
                    "excluded_paths": [],
                },
                enrich_config=lambda: {
                    "project_version_descriptor": {
                        **project_version_state,
                        "excluded_paths": [],
                    },
                    "log_level": "INFO",
                },
            )
            mock_configure.return_value = {
                "git": {"resolved_commit": resolved_commit},
                "output_dir": "/aist-output",
                "project_path": "/aist-project",
                "trim_path": "",
                "tmp_analyzer_config_path": "/aist-analyzers.yml",
            }

            run_sast_pipeline.run(pipeline.id, {"project_id": self.project.id})

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
            patch("aist.tasks.pipeline.PipelineArguments.from_dict") as mock_from_dict,
            patch("aist.tasks.pipeline.AISTProjectVersion.ensure_extracted", return_value=None),
            patch("aist.tasks.pipeline.get_project_build_path", return_value="/aist-project"),
            patch("aist.tasks.pipeline.install_pipeline_logging", return_value=_DummyLogger()),
            patch("aist.tasks.pipeline.AnalyzersConfigHelper"),
            patch("aist.tasks.pipeline.configure_project_run_analyses") as mock_configure,
            patch("aist.tasks.pipeline.upload_results_internal", return_value=[SimpleNamespace(test_id=dd_test.id)]),
            patch("aist.tasks.pipeline.postprocess_findings", return_value=SimpleNamespace()) as mock_postprocess,
            patch("aist.tasks.pipeline.Finding.objects.filter") as mock_finding_filter,
        ):
            project_version_state = {"id": branch.id, "version": "main", "type": VersionType.GIT_BRANCH}

            def _resolve_effective_project_version(*, resolved_commit=""):
                commit = (resolved_commit or "").strip()
                pv_id = project_version_state.get("id")
                current_project_version = AISTProjectVersion.objects.filter(pk=pv_id).first() if pv_id else None
                if current_project_version and commit and current_project_version.version_type == VersionType.GIT_BRANCH:
                    resolved, _ = AISTProjectVersion.objects.get_or_create(
                        project_id=current_project_version.project_id,
                        version=commit,
                        version_type=VersionType.GIT_HASH,
                        defaults={"resolved_from_branch": current_project_version},
                    )
                    if resolved.resolved_from_branch_id is None:
                        resolved.resolved_from_branch = current_project_version
                        resolved.save(update_fields=["resolved_from_branch", "updated"])
                    current_project_version.last_resolved_commit = commit
                    current_project_version.save(update_fields=["last_resolved_commit", "updated"])
                    project_version_state.update(resolved.as_dict())
                    return resolved
                return current_project_version

            def _finding_filter(*args, **kwargs):
                if "test_id__in" in kwargs:
                    return SimpleNamespace(values_list=lambda *_args, **_kwargs: [finding.id, missing_finding_id])
                return original_finding_filter(*args, **kwargs)

            mock_finding_filter.side_effect = _finding_filter
            mock_from_dict.return_value = SimpleNamespace(
                project_version=project_version_state,
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
                resolve_effective_project_version=_resolve_effective_project_version,
                build_project_version_descriptor=lambda: {
                    **project_version_state,
                    "excluded_paths": [],
                },
                enrich_config=lambda: {
                    "project_version_descriptor": {
                        **project_version_state,
                        "excluded_paths": [],
                    },
                    "log_level": "INFO",
                },
            )
            mock_configure.return_value = {
                "git": {"resolved_commit": resolved_commit},
                "output_dir": "/aist-output",
                "project_path": "/aist-project",
                "trim_path": "",
                "tmp_analyzer_config_path": "/aist-analyzers.yml",
            }

            run_sast_pipeline.run(pipeline.id, {"project_id": self.project.id})

        pipeline.refresh_from_db()
        branch.refresh_from_db()

        hash_version = pipeline.project_version
        self.assertEqual(hash_version.version_type, VersionType.GIT_HASH)
        self.assertTrue(hash_version.findings.filter(id=finding.id).exists())
        self.assertFalse(hash_version.findings.filter(id=missing_finding_id).exists())
        self.assertTrue(branch.findings.filter(id=finding.id).exists())
        self.assertFalse(branch.findings.filter(id=missing_finding_id).exists())
        mock_postprocess.assert_called_once_with(pipeline.id, "INFO")
