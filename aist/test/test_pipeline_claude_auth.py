"""
The generic pipeline worker resolves the project's Claude integration
alongside the existing VPN resolution and forwards the resulting generic
``auth_env`` dict to ``build_bridge_client_from_settings``.

Architectural invariants verified here:
- I1 — ``aist.tasks.sast_execution`` does not embed a Claude env-var literal;
  the resolution call goes through ``aist.integrations.claude.claude_subprocess_env``.
- I4 — the factory is invoked with a ready dict, not a project; the
  resolution happens in ``sast_execution.py``.

Sole test fixture: a minimal pipeline run with all expensive collaborators
mocked. The assertion target is the ``auth_env`` kwarg passed to
``build_bridge_client_from_settings``.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from aist.models import (
    AISTPipeline,
    AISTProjectVersion,
    Organization,
    OrgIntegration,
    OrgIntegrationType,
    VersionType,
)
from aist.test.pipeline_execution_helpers import SastWorkerTestMixin
from aist.test.test_api import AISTApiBase


class PipelineClaudeAuthEnvTests(SastWorkerTestMixin, AISTApiBase):

    def setUp(self):
        super().setUp()
        self.org_prod_type = self.prod_type
        self.org = Organization.objects.create(
            name="Pipeline Claude Org",
            product_type=self.org_prod_type,
        )
        self.project.refresh_from_db()
        self.branch = AISTProjectVersion.objects.create(
            project=self.project,
            version_type=VersionType.GIT_BRANCH,
            version="main",
        )
        self.pipeline = AISTPipeline.objects.create(
            id="pipe-claude-auth-1",
            project=self.project,
            project_version=self.branch,
            status="FINISHED",
        )

    def _run_with_mocks(self) -> MagicMock:
        """Run the pipeline and return the bridge client factory mock for inspection."""
        with patch("aist.tasks.sast_execution.build_bridge_client_from_settings") as mock_factory:
            mock_factory.return_value = MagicMock()
            self.run_sast(self.pipeline, project_version=self.branch, upload_results=[])
            return mock_factory

    def test_factory_called_with_claude_oauth_token_when_integration_configured(self):
        OrgIntegration.objects.create(
            organization=self.org,
            integration_type=OrgIntegrationType.CLAUDE_CODE,
            name="primary",
            secret="sk-ant-oat01-pipeline-test-value-1234567890",  # noqa: S106
            is_active=True,
            config={"auth_mode": "oauth"},
        )

        mock_factory = self._run_with_mocks()

        mock_factory.assert_called_once()
        kwargs = mock_factory.call_args.kwargs
        auth_env = kwargs.get("auth_env") or {}
        self.assertEqual(
            auth_env,
            {"CLAUDE_CODE_OAUTH_TOKEN": "sk-ant-oat01-pipeline-test-value-1234567890"},
        )

    def test_factory_called_with_empty_auth_env_when_no_integration(self):
        # No Claude OrgIntegration on org → factory must still be called,
        # but with auth_env empty. The bridge will return per-call errors
        # for agent-bridge analyzers; non-agent analyzers continue normally.
        mock_factory = self._run_with_mocks()

        mock_factory.assert_called_once()
        kwargs = mock_factory.call_args.kwargs
        auth_env = kwargs.get("auth_env") or {}
        self.assertEqual(auth_env, {})

    def test_factory_called_with_empty_auth_env_when_integration_inactive(self):
        OrgIntegration.objects.create(
            organization=self.org,
            integration_type=OrgIntegrationType.CLAUDE_CODE,
            name="disabled",
            secret="sk-ant-oat01-inactive-token-should-be-ignored",  # noqa: S106
            is_active=False,
            config={"auth_mode": "oauth"},
        )

        mock_factory = self._run_with_mocks()

        kwargs = mock_factory.call_args.kwargs
        self.assertEqual(kwargs.get("auth_env") or {}, {})
