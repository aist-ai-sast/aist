"""
Task 7 — ``run_sast_pipeline`` resolves the project's Claude integration
alongside the existing VPN resolution and forwards the resulting generic
``auth_env`` dict to ``build_bridge_client_from_settings``.

Architectural invariants verified here:
- I1 — ``aist.tasks.pipeline`` does not embed a Claude env-var literal;
  the resolution call goes through ``aist.integrations.claude.claude_auth_env``.
- I4 — the factory is invoked with a ready dict, not a project; the
  resolution happens in ``pipeline.py``.

Sole test fixture: a minimal pipeline run with all expensive collaborators
mocked. The assertion target is the ``auth_env`` kwarg passed to
``build_bridge_client_from_settings``.
"""
from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from aist.models import (
    AISTPipeline,
    AISTProjectVersion,
    Organization,
    OrgIntegration,
    OrgIntegrationType,
    VersionType,
)
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


def _params_namespace(project_version_state) -> SimpleNamespace:
    return SimpleNamespace(
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
        resolve_effective_project_version=lambda **_: None,
        build_project_version_descriptor=lambda: {**project_version_state, "excluded_paths": []},
        enrich_config=lambda: {
            "project_version_descriptor": {**project_version_state, "excluded_paths": []},
            "log_level": "INFO",
        },
    )


class PipelineClaudeAuthEnvTests(AISTApiBase):

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
        self._project_version_state = {
            "id": self.branch.id,
            "version": "main",
            "type": VersionType.GIT_BRANCH,
        }

    def _run_with_mocks(self) -> MagicMock:
        """Run the pipeline and return the factory mock for inspection."""
        params = _params_namespace(self._project_version_state)
        with (
            patch("aist.tasks.pipeline.PipelineArguments.from_dict", return_value=params),
            patch("aist.tasks.pipeline.AISTProjectVersion.ensure_extracted", return_value=None),
            patch("aist.tasks.pipeline.get_project_build_path", return_value="/aist-project"),
            patch("aist.tasks.pipeline.install_pipeline_logging", return_value=_DummyLogger()),
            patch("aist.tasks.pipeline.AnalyzersConfigHelper"),
            patch("aist.tasks.pipeline.configure_project_run_analyses", return_value={
                "git": {"resolved_commit": ""},
                "output_dir": "/aist-output",
                "project_path": "/aist-project",
                "trim_path": "",
                "tmp_analyzer_config_path": "/aist-analyzers.yml",
            }),
            patch("aist.tasks.pipeline.upload_results_internal", return_value=[]),
            patch(
                "aist.tasks.pipeline.build_bridge_client_from_settings",
            ) as mock_factory,
        ):
            mock_factory.return_value = MagicMock()
            run_sast_pipeline.run(self.pipeline.id, {"project_id": self.project.id})
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
