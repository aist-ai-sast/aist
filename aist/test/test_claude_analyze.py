from __future__ import annotations

from contextlib import contextmanager
from unittest.mock import MagicMock, patch

from aist.api.github_integration import GithubImportExecuteSerializer
from aist.api.gitlab_integration import ImportGitlabRequestSerializer
from aist.models import (
    AISTProjectVersion,
    Organization,
    OrgIntegration,
    OrgIntegrationType,
    RepositoryInfo,
    ScmType,
    VersionType,
)
from aist.tasks.claude import _send_to_bridge, analyze_project_after_import
from aist.test.test_api import AISTApiBase


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
        self.claude_integration = OrgIntegration.objects.create(
            organization=self.org,
            integration_type=OrgIntegrationType.CLAUDE_CODE,
            name="primary",
            secret="sk-ant-oat01-post-import-test-token-abc1234",  # noqa: S106
            is_active=True,
            config={"auth_mode": "oauth"},
        )
        self.main = AISTProjectVersion.objects.create(
            project=self.project, version="main", version_type=VersionType.GIT_BRANCH,
        )

    @patch("aist.tasks.claude._send_to_bridge")
    @patch("aist.tasks.claude.subprocess")
    @patch("aist.tasks.claude.vpn_sidecar_context", _fake_vpn_ctx)
    @patch("aist.tasks.claude.resolve_integration", return_value=None)
    def test_clones_requested_branch_and_binds_generator_to_it(self, mock_resolve, mock_subprocess, mock_bridge):
        mock_bridge.return_value = True
        AISTProjectVersion.objects.create(
            project=self.project, version="develop", version_type=VersionType.GIT_BRANCH,
        )

        analyze_project_after_import(self.project.id, self.main.id)

        mock_subprocess.run.assert_called_once()
        argv = mock_subprocess.run.call_args[0][0]
        self.assertEqual(argv[:6], ["git", "clone", "--depth=1", "--branch", "main", "--"])
        self.assertEqual(argv[6], self.repo_info.clone_url)
        self.assertTrue(argv[7].endswith(f"claude-analysis-{self.main.id}"))
        for bridge_call in mock_bridge.call_args_list:
            self.assertEqual(bridge_call[1]["source_path"], argv[7])

        self.assertEqual(mock_bridge.call_count, 2)
        calls = mock_bridge.call_args_list
        self.assertEqual(calls[0][1]["skill_name"], "aist-init-script-generator")
        self.assertEqual(calls[0][1]["extra_args"], f"project_version_id={self.main.id}")
        # Exclusions stay project-level: the profile analyzer gets no version.
        self.assertEqual(calls[1][1]["skill_name"], "aist-project-profile-analyzer")
        self.assertNotIn("extra_args", calls[1][1])
        # Per Task 8 — the bridge payload must carry the Claude token via
        # the generic subprocess_env channel introduced in Task 4. Both
        # calls share the same env mapping for this project.
        for call in calls:
            self.assertEqual(
                call[1]["subprocess_env"],
                {"CLAUDE_CODE_OAUTH_TOKEN": "sk-ant-oat01-post-import-test-token-abc1234"},
            )

    @patch("aist.tasks.claude._send_to_bridge")
    @patch("aist.tasks.claude.subprocess.run", side_effect=Exception("clone failed"))
    @patch("aist.tasks.claude.vpn_sidecar_context", _fake_vpn_ctx)
    @patch("aist.tasks.claude.resolve_integration", return_value=None)
    def test_clone_failure_does_not_call_bridge(self, mock_resolve, mock_run, mock_bridge):
        analyze_project_after_import(self.project.id, self.main.id)

        mock_bridge.assert_not_called()

    @patch("aist.tasks.claude._send_to_bridge")
    @patch("aist.tasks.claude.subprocess")
    @patch("aist.tasks.claude.vpn_sidecar_context", _fake_vpn_ctx)
    @patch("aist.tasks.claude.resolve_integration", return_value=None)
    def test_skipped_when_no_active_claude_integration(self, mock_resolve, mock_subprocess, mock_bridge):
        # Deactivate the integration set up by setUp() — auto-analyze
        # must short-circuit BEFORE cloning so we don't waste git/disk
        # for a run that has no chance of succeeding.
        self.claude_integration.is_active = False
        self.claude_integration.save(update_fields=["is_active"])

        analyze_project_after_import(self.project.id, self.main.id)

        mock_subprocess.run.assert_not_called()
        mock_bridge.assert_not_called()

    @patch("aist.tasks.claude._send_to_bridge")
    @patch("aist.tasks.claude.subprocess")
    def test_rejects_version_that_is_not_a_branch_of_this_project(self, mock_subprocess, mock_bridge):
        other_branch = AISTProjectVersion.objects.create(
            project=self.other_project, version="main", version_type=VersionType.GIT_BRANCH,
        )
        # self.pv is a GIT_HASH version of this project.
        for version_id in (self.pv.id, other_branch.id, 999999):
            with self.subTest(version_id=version_id), self.assertLogs("aist.tasks.claude", level="ERROR"):
                analyze_project_after_import(self.project.id, version_id)

        mock_subprocess.run.assert_not_called()
        mock_bridge.assert_not_called()

    def test_nonexistent_project_is_noop(self):
        analyze_project_after_import(999999, self.main.id)

    def test_project_without_repo_is_noop(self):
        self.project.repository = None
        self.project.save(update_fields=["repository"])
        analyze_project_after_import(self.project.id, self.main.id)


class SendToBridgeTests(AISTApiBase):

    """Tests for _send_to_bridge helper."""

    @patch("aist.tasks.claude.httpx")
    def test_success(self, mock_httpx):
        mock_client = MagicMock()
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.post.return_value = mock_resp
        mock_httpx.Client.return_value = mock_client
        mock_httpx.HTTPTransport.return_value = MagicMock()

        result = _send_to_bridge(
            skill_name="test-skill",
            project_id=1,
            source_path="/tmp/test",  # noqa: S108
            subprocess_env={"CLAUDE_CODE_OAUTH_TOKEN": "sk-ant-oat01-test-value"},
        )
        self.assertTrue(result)
        mock_client.post.assert_called_once()
        payload = mock_client.post.call_args[1]["json"]
        self.assertEqual(payload["skill_name"], "test-skill")
        self.assertEqual(payload["project_id"], "1")
        # Task 4 generic field — bridge merges into spawn env.
        self.assertEqual(payload["subprocess_env"], {"CLAUDE_CODE_OAUTH_TOKEN": "sk-ant-oat01-test-value"})
        self.assertEqual(payload["extra_args"], "")

    @patch("aist.tasks.claude.httpx")
    def test_extra_args_reach_bridge_payload(self, mock_httpx):
        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_httpx.Client.return_value = mock_client

        _send_to_bridge(
            skill_name="aist-init-script-generator",
            project_id=1,
            source_path="/tmp/test",  # noqa: S108
            extra_args="project_version_id=7",
        )

        self.assertEqual(mock_client.post.call_args[1]["json"]["extra_args"], "project_version_id=7")

    @patch("aist.tasks.claude.httpx")
    def test_failure_returns_false(self, mock_httpx):
        mock_httpx.HTTPTransport.side_effect = Exception("socket missing")

        result = _send_to_bridge(
            skill_name="test-skill",
            project_id=1,
            source_path="/tmp/test",  # noqa: S108
            subprocess_env={},
        )
        self.assertFalse(result)


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
