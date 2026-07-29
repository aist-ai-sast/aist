"""
build_diff_env(pipeline) prepares runtime config passed to diff-aware
agent analyzers through configure_project_run_analyses. It only does DB work — the L2/L3 BASE fallbacks
(14-day window, first-ever commit) are the skill's job because they
need the cloned repo.
"""
from __future__ import annotations

import json

from aist.models import AISTPipeline, AISTProjectVersion, AISTStatus, VersionType
from aist.test.test_api import AISTApiBase
from aist.utils.diff_baseline import build_diff_env, get_prior_successful_commit


class GetPriorSuccessfulCommitTests(AISTApiBase):

    def setUp(self):
        super().setUp()
        # Parent GIT_BRANCH version on the project.
        self.branch_pv = AISTProjectVersion.objects.create(
            project=self.project,
            version_type=VersionType.GIT_BRANCH,
            version="main",
        )
        # A child GIT_HASH version of an earlier commit on `main`.
        self.prior_hash_pv = AISTProjectVersion.objects.create(
            project=self.project,
            version_type=VersionType.GIT_HASH,
            version="abc1234567890abc1234567890abc1234567890a",
            last_resolved_commit="abc1234567890abc1234567890abc1234567890a",
            resolved_from_branch=self.branch_pv,
        )

    def _make_pipeline(self, *, project_version, status, pipeline_id):
        return AISTPipeline.objects.create(
            id=pipeline_id,
            project=self.project,
            project_version=project_version,
            status=status,
        )

    def test_returns_prior_pipeline_commit_when_current_is_branch(self):
        # Prior FINISHED pipeline on the same branch via a GIT_HASH child.
        self._make_pipeline(
            project_version=self.prior_hash_pv,
            status=AISTStatus.FINISHED,
            pipeline_id="pipe-prior",
        )
        current = self._make_pipeline(
            project_version=self.branch_pv,
            status=AISTStatus.EXECUTING,
            pipeline_id="pipe-current-branch",
        )
        self.assertEqual(get_prior_successful_commit(current), self.prior_hash_pv.last_resolved_commit)

    def test_returns_prior_pipeline_commit_when_current_is_git_hash(self):
        # Current pipeline already resolved to a GIT_HASH; prior pipeline still
        # on the same parent branch must be located via resolved_from_branch.
        self._make_pipeline(
            project_version=self.prior_hash_pv,
            status=AISTStatus.FINISHED_WITH_WARNINGS,
            pipeline_id="pipe-prior-2",
        )
        current_hash_pv = AISTProjectVersion.objects.create(
            project=self.project,
            version_type=VersionType.GIT_HASH,
            version="def5678901def5678901def5678901def5678901",
            last_resolved_commit="def5678901def5678901def5678901def5678901",
            resolved_from_branch=self.branch_pv,
        )
        current = self._make_pipeline(
            project_version=current_hash_pv,
            status=AISTStatus.EXECUTING,
            pipeline_id="pipe-current-hash",
        )
        self.assertEqual(get_prior_successful_commit(current), self.prior_hash_pv.last_resolved_commit)

    def test_returns_none_when_no_prior_pipeline(self):
        current = self._make_pipeline(
            project_version=self.branch_pv,
            status=AISTStatus.EXECUTING,
            pipeline_id="pipe-no-prior",
        )
        self.assertIsNone(get_prior_successful_commit(current))

    def test_skips_non_terminal_pipelines(self):
        # A pipeline still running on the same branch must not be picked.
        self._make_pipeline(
            project_version=self.prior_hash_pv,
            status=AISTStatus.EXECUTING,
            pipeline_id="pipe-running",
        )
        current = self._make_pipeline(
            project_version=self.branch_pv,
            status=AISTStatus.EXECUTING,
            pipeline_id="pipe-current-only",
        )
        self.assertIsNone(get_prior_successful_commit(current))

    def test_does_not_cross_organizations_or_projects(self):
        # A FINISHED pipeline on a DIFFERENT project must never be picked.
        other_branch = AISTProjectVersion.objects.create(
            project=self.other_project,
            version_type=VersionType.GIT_BRANCH,
            version="main",
        )
        other_hash = AISTProjectVersion.objects.create(
            project=self.other_project,
            version_type=VersionType.GIT_HASH,
            version="bbb",
            last_resolved_commit="bbb",
            resolved_from_branch=other_branch,
        )
        AISTPipeline.objects.create(
            id="pipe-other-project",
            project=self.other_project,
            project_version=other_hash,
            status=AISTStatus.FINISHED,
        )
        current = self._make_pipeline(
            project_version=self.branch_pv,
            status=AISTStatus.EXECUTING,
            pipeline_id="pipe-iso",
        )
        self.assertIsNone(get_prior_successful_commit(current))

    def test_returns_none_for_file_hash_version(self):
        file_pv = AISTProjectVersion.objects.create(
            project=self.project,
            version_type=VersionType.FILE_HASH,
            version="archive.zip",
        )
        current = self._make_pipeline(
            project_version=file_pv,
            status=AISTStatus.EXECUTING,
            pipeline_id="pipe-file-hash",
        )
        self.assertIsNone(get_prior_successful_commit(current))


class BuildDiffEnvTests(AISTApiBase):

    def setUp(self):
        super().setUp()
        self.project.profile = {"paths": {"exclude": ["vendor/", "third_party/"]}}
        self.project.save(update_fields=["profile"])

        self.branch_pv = AISTProjectVersion.objects.create(
            project=self.project,
            version_type=VersionType.GIT_BRANCH,
            version="main",
        )
        self.pipeline = AISTPipeline.objects.create(
            id="pipe-build-env",
            project=self.project,
            project_version=self.branch_pv,
            status=AISTStatus.EXECUTING,
        )

    def test_returns_all_required_keys_with_string_values(self):
        env = build_diff_env(self.pipeline)
        for key in ("BASE_COMMIT", "EXCLUDED_PATHS_JSON", "CLAUDE_DIFF_MAX_FILES", "CLAUDE_DIFF_MAX_BYTES"):
            self.assertIn(key, env)
            self.assertIsInstance(env[key], str)

    def test_excluded_paths_serialized_as_json_array(self):
        env = build_diff_env(self.pipeline)
        decoded = json.loads(env["EXCLUDED_PATHS_JSON"])
        self.assertEqual(sorted(decoded), ["third_party/", "vendor/"])

    def test_base_commit_empty_when_no_prior_pipeline(self):
        env = build_diff_env(self.pipeline)
        self.assertEqual(env["BASE_COMMIT"], "")

    def test_max_files_and_bytes_come_from_django_settings(self):
        with self.settings(CLAUDE_DIFF_MAX_FILES=17, CLAUDE_DIFF_MAX_BYTES=4096):
            env = build_diff_env(self.pipeline)
        self.assertEqual(env["CLAUDE_DIFF_MAX_FILES"], "17")
        self.assertEqual(env["CLAUDE_DIFF_MAX_BYTES"], "4096")

    def test_max_files_and_bytes_reject_invalid_settings(self):
        with self.settings(CLAUDE_DIFF_MAX_FILES=0), self.assertRaises(ValueError):
            build_diff_env(self.pipeline)
