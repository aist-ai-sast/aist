from __future__ import annotations

from contextlib import contextmanager
from unittest.mock import patch

from aist.api.github_integration import GithubImportExecuteSerializer
from aist.api.gitlab_integration import ImportGitlabRequestSerializer
from aist.models import (
    AISTProjectVersion,
    Organization,
    RepositoryInfo,
    ScmType,
    VersionType,
)
from aist.tasks.claude import analyze_project_after_import
from aist.test.pipeline_execution_helpers import make_claude_integration
from aist.test.test_api import AISTApiBase
from aist.utils.bridge_client_factory import BridgeError


@contextmanager
def _fake_vpn_ctx(*_args, **_kwargs):
    yield (None, None)


class AnalyzeProjectAfterImportTests(AISTApiBase):

    """Tests for analyze_project_after_import Celery task."""

    def setUp(self):
        super().setUp()
        self.repo_info = RepositoryInfo.objects.create(
            type=ScmType.GITHUB,
            repo_owner="test-owner",
            repo_name="test-repo",
            base_url="https://github.com",
        )
        self.project.repository = self.repo_info
        # Project needs an Organization to be eligible for a Claude
        # OrgIntegration. Task 8 makes the auto-analyze flow skip the
        # whole pipeline if no integration is found, so tests that
        # expect bridge calls must wire one up here.
        self.org_prod_type = self.prod_type
        self.org = Organization.objects.create(
            name="Claude Analyze Org",
            product_type=self.org_prod_type,
        )
        self.project.refresh_from_db()
        self.project.repository = self.repo_info
        self.project.save(update_fields=["repository"])
        self.claude_integration = make_claude_integration(
            self.org,
            secret="sk-ant-oat01-post-import-test-token-abc1234",  # noqa: S106
        )
        self.main = AISTProjectVersion.objects.create(
            project=self.project, version="main", version_type=VersionType.GIT_BRANCH,
        )

    @patch("aist.tasks.claude.build_bridge_client_from_settings")
    @patch("aist.tasks.claude.subprocess")
    @patch("aist.tasks.claude.vpn_sidecar_context", _fake_vpn_ctx)
    @patch("aist.tasks.claude.resolve_source_vpn", return_value=None)
    def test_clones_requested_branch_and_binds_generator_to_it(self, mock_resolve, mock_subprocess, mock_factory):
        bridge = mock_factory.return_value
        AISTProjectVersion.objects.create(
            project=self.project, version="develop", version_type=VersionType.GIT_BRANCH,
        )

        analyze_project_after_import(self.project.id, self.main.id)

        mock_subprocess.run.assert_called_once()
        argv = mock_subprocess.run.call_args[0][0]
        self.assertEqual(argv[:6], ["git", "clone", "--depth=1", "--branch", "main", "--"])
        self.assertEqual(argv[6], self.repo_info.clone_url)
        self.assertTrue(argv[7].endswith(f"claude-analysis-{self.main.id}"))
        calls = bridge.analyze_async.call_args_list
        for bridge_call in calls:
            self.assertEqual(bridge_call.kwargs["source_path"], argv[7])
            self.assertEqual(bridge_call.kwargs["project_id"], str(self.project.id))

        self.assertEqual(
            [(call.kwargs["skill_name"], call.kwargs["extra_args"]) for call in calls],
            [
                ("aist-init-script-generator", f"project_version_id={self.main.id}"),
                # Exclusions stay project-level: the profile analyzer gets no version.
                ("aist-project-profile-analyzer", ""),
            ],
        )
        # Both sessions run with the project organization's Claude token.
        mock_factory.assert_called_once_with(
            auth_env={"CLAUDE_CODE_OAUTH_TOKEN": "sk-ant-oat01-post-import-test-token-abc1234"},
        )

    @patch("aist.tasks.claude.build_bridge_client_from_settings")
    @patch("aist.tasks.claude.subprocess")
    @patch("aist.tasks.claude.vpn_sidecar_context", _fake_vpn_ctx)
    @patch("aist.tasks.claude.resolve_source_vpn", return_value=None)
    def test_a_refused_init_script_request_still_sends_the_profile_analysis(self, mock_resolve, mock_subprocess, mock_factory):
        bridge = mock_factory.return_value
        bridge.analyze_async.side_effect = [BridgeError("bridge socket missing"), 202]

        with self.assertLogs("aist.tasks.claude", level="ERROR") as logs:
            analyze_project_after_import(self.project.id, self.main.id)

        self.assertEqual(
            [call.kwargs["skill_name"] for call in bridge.analyze_async.call_args_list],
            ["aist-init-script-generator", "aist-project-profile-analyzer"],
        )
        self.assertIn("aist-init-script-generator", "\n".join(logs.output))

    @patch("aist.tasks.claude.build_bridge_client_from_settings")
    @patch("aist.tasks.claude.subprocess.run", side_effect=Exception("clone failed"))
    @patch("aist.tasks.claude.vpn_sidecar_context", _fake_vpn_ctx)
    @patch("aist.tasks.claude.resolve_source_vpn", return_value=None)
    def test_clone_failure_does_not_call_bridge(self, mock_resolve, mock_run, mock_factory):
        analyze_project_after_import(self.project.id, self.main.id)

        mock_factory.assert_not_called()

    @patch("aist.tasks.claude.build_bridge_client_from_settings")
    @patch("aist.tasks.claude.subprocess")
    @patch("aist.tasks.claude.vpn_sidecar_context", _fake_vpn_ctx)
    @patch("aist.tasks.claude.resolve_source_vpn", return_value=None)
    def test_skipped_when_no_active_claude_integration(self, mock_resolve, mock_subprocess, mock_factory):
        # Deactivate the integration set up by setUp() — auto-analyze
        # must short-circuit BEFORE cloning so we don't waste git/disk
        # for a run that has no chance of succeeding.
        self.claude_integration.is_active = False
        self.claude_integration.save(update_fields=["is_active"])

        analyze_project_after_import(self.project.id, self.main.id)

        mock_subprocess.run.assert_not_called()
        mock_factory.assert_not_called()

    @patch("aist.tasks.claude.build_bridge_client_from_settings")
    @patch("aist.tasks.claude.subprocess")
    def test_rejects_version_that_is_not_a_branch_of_this_project(self, mock_subprocess, mock_factory):
        other_branch = AISTProjectVersion.objects.create(
            project=self.other_project, version="main", version_type=VersionType.GIT_BRANCH,
        )
        # self.pv is a GIT_HASH version of this project.
        for version_id in (self.pv.id, other_branch.id, 999999):
            with self.subTest(version_id=version_id), self.assertLogs("aist.tasks.claude", level="ERROR"):
                analyze_project_after_import(self.project.id, version_id)

        mock_subprocess.run.assert_not_called()
        mock_factory.assert_not_called()

    def test_nonexistent_project_is_noop(self):
        analyze_project_after_import(999999, self.main.id)

    def test_project_without_repo_is_noop(self):
        self.project.repository = None
        self.project.save(update_fields=["repository"])
        analyze_project_after_import(self.project.id, self.main.id)


class GithubImportAutoAnalyzeSerializerTests(AISTApiBase):

    """Tests for auto_analyze field in GithubImportExecuteSerializer."""

    def test_auto_analyze_defaults_to_false(self):
        data = {
            "organization_id": 1,
            "installation_id": 123,
            "repositories": ["owner/repo"],
        }
        s = GithubImportExecuteSerializer(data=data)
        self.assertTrue(s.is_valid(), s.errors)
        self.assertFalse(s.validated_data["auto_analyze"])

    def test_auto_analyze_true(self):
        data = {
            "organization_id": 1,
            "installation_id": 123,
            "repositories": ["owner/repo"],
            "auto_analyze": True,
        }
        s = GithubImportExecuteSerializer(data=data)
        self.assertTrue(s.is_valid(), s.errors)
        self.assertTrue(s.validated_data["auto_analyze"])


class GitlabImportAutoAnalyzeSerializerTests(AISTApiBase):

    """Tests for auto_analyze field in ImportGitlabRequestSerializer."""

    def test_auto_analyze_defaults_to_false(self):
        data = {"project_id": 42, "organization_id": 1}
        s = ImportGitlabRequestSerializer(data=data)
        self.assertTrue(s.is_valid(), s.errors)
        self.assertFalse(s.validated_data["auto_analyze"])

    def test_auto_analyze_true(self):
        data = {"project_id": 42, "organization_id": 1, "auto_analyze": True}
        s = ImportGitlabRequestSerializer(data=data)
        self.assertTrue(s.is_valid(), s.errors)
        self.assertTrue(s.validated_data["auto_analyze"])
