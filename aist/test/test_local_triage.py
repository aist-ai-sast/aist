from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from django.urls import reverse
from django.utils import timezone
from dojo.models import Engagement, Finding, Test, Test_Type

from aist.launch_data import PipelineLaunchData
from aist.models import AISTAIFindingResponse, AISTPipeline, AISTStatus
from aist.pipeline_args import PipelineArguments
from aist.profile import ProjectProfile
from aist.tasks.ai import (
    AutoPushOutcome,
    _prepare_auto_push,
    auto_push_to_ai_if_configured,
    push_request_to_local_triage,
    resolve_triage_type,
)
from aist.tasks.enrich import after_upload_enrich_and_watch
from aist.test.pipeline_execution_helpers import SastWorkerTestMixin
from aist.test.test_api import AISTApiBase


def _noop_logger():
    return SimpleNamespace(
        info=MagicMock(), warning=MagicMock(), error=MagicMock(),
    )


class ProjectProfileAiTriageTests(AISTApiBase):

    """Tests for the AiTriageConfig extension of ProjectProfile."""

    def test_default_triage_type_is_n8n(self):
        profile = ProjectProfile.from_dict(None)
        self.assertEqual(profile.ai_triage.type, "n8n")

    def test_from_dict_with_ai_triage_local(self):
        profile = ProjectProfile.from_dict({"ai_triage": {"type": "local"}})
        self.assertEqual(profile.ai_triage.type, "local")

    def test_from_dict_with_ai_triage_n8n(self):
        profile = ProjectProfile.from_dict({"ai_triage": {"type": "n8n"}})
        self.assertEqual(profile.ai_triage.type, "n8n")

    def test_from_dict_with_invalid_triage_type_falls_back_to_n8n(self):
        profile = ProjectProfile.from_dict({"ai_triage": {"type": "invalid"}})
        self.assertEqual(profile.ai_triage.type, "n8n")

    def test_from_dict_empty_ai_triage(self):
        profile = ProjectProfile.from_dict({"ai_triage": {}})
        self.assertEqual(profile.ai_triage.type, "n8n")

    def test_validate_dict_accepts_valid_ai_triage(self):
        ProjectProfile.validate_dict({"ai_triage": {"type": "local"}})
        ProjectProfile.validate_dict({"ai_triage": {"type": "n8n"}})

    def test_validate_dict_rejects_invalid_triage_type(self):
        with self.assertRaises(ValueError):
            ProjectProfile.validate_dict({"ai_triage": {"type": "bad"}})

    def test_validate_dict_rejects_non_dict_ai_triage(self):
        with self.assertRaises(ValueError):
            ProjectProfile.validate_dict({"ai_triage": "local"})


class ResolveTriageTypeTests(AISTApiBase):

    """Tests for resolve_triage_type."""

    def _make_pipeline(self, *, profile=None, launch_ai=None):
        if profile is not None:
            self.project.profile = profile
            self.project.save(update_fields=["profile"])
        pipeline = AISTPipeline.objects.create(
            project=self.project,
            project_version=self.pv,
            status=AISTStatus.PUSH_TO_AI,
            launch_data={"ai": launch_ai or {}},
        )
        pipeline.project = self.project  # ensure select_related-like access
        return pipeline

    def test_default_is_n8n(self):
        pipeline = self._make_pipeline()
        self.assertEqual(resolve_triage_type(pipeline), "n8n")

    def test_project_profile_local(self):
        pipeline = self._make_pipeline(profile={"ai_triage": {"type": "local"}})
        self.assertEqual(resolve_triage_type(pipeline), "local")

    def test_per_launch_overrides_project(self):
        pipeline = self._make_pipeline(
            profile={"ai_triage": {"type": "n8n"}},
            launch_ai={"triage_type": "local"},
        )
        self.assertEqual(resolve_triage_type(pipeline), "local")

    def test_per_launch_n8n_overrides_project_local(self):
        pipeline = self._make_pipeline(
            profile={"ai_triage": {"type": "local"}},
            launch_ai={"triage_type": "n8n"},
        )
        self.assertEqual(resolve_triage_type(pipeline), "n8n")


class LocalTriageCompleteAPITests(AISTApiBase):

    """Tests for the LocalTriageCompleteAPI endpoint."""

    def setUp(self):
        super().setUp()
        self.login_as_superuser()

    @patch("aist.api.ai.finish_pipeline")
    def test_rejects_ordinary_authenticated_user(self, mock_finish):
        ordinary_user = type(self.user).objects.create_user(
            username="ordinary_local_callback",
            email="ordinary_local_callback@example.com",
            password="pass",  # noqa: S106
        )
        self.client.force_authenticate(user=ordinary_user)
        pipeline = AISTPipeline.objects.create(
            id="local-pipe-ordinary-user",
            project=self.project,
            project_version=self.pv,
            status=AISTStatus.WAITING_RESULT_FROM_AI,
        )

        resp = self.client.post(
            reverse("aist_api:pipeline_local_triage_complete", kwargs={"pipeline_id": pipeline.id}),
            data={"status": "success"},
            format="json",
        )

        self.assertEqual(resp.status_code, 403)
        mock_finish.assert_not_called()

    @patch("aist.api.ai.finish_pipeline")
    def test_success_callback(self, mock_finish):
        pipeline = AISTPipeline.objects.create(
            id="local-pipe-1",
            project=self.project,
            project_version=self.pv,
            status=AISTStatus.WAITING_RESULT_FROM_AI,
        )
        url = reverse("aist_api:pipeline_local_triage_complete", kwargs={"pipeline_id": pipeline.id})
        resp = self.client.post(url, data={"status": "success"}, format="json")

        self.assertEqual(resp.status_code, 200)
        mock_finish.assert_called_once_with("local-pipe-1", degraded=False)

    @patch("aist.api.ai.finish_pipeline")
    def test_error_callback_without_responses_is_degraded(self, mock_finish):
        """Bridge error with no AI responses persisted → pipeline is degraded."""
        pipeline = AISTPipeline.objects.create(
            id="local-pipe-2",
            project=self.project,
            project_version=self.pv,
            status=AISTStatus.WAITING_RESULT_FROM_AI,
        )
        url = reverse("aist_api:pipeline_local_triage_complete", kwargs={"pipeline_id": pipeline.id})
        resp = self.client.post(
            url,
            data={"status": "error", "detail": "claude timed out"},
            format="json",
        )

        self.assertEqual(resp.status_code, 200)
        mock_finish.assert_called_once_with("local-pipe-2", degraded=True)

    @patch("aist.api.ai.finish_pipeline")
    def test_error_callback_with_existing_responses_is_not_degraded(self, mock_finish):
        """
        Regression (pipeline d5d0aa24): bridge timed out after claude -p already
        wrote the triage facts. The callback says ``error`` but the pipeline has
        218 AISTAIFindingResponse rows — we must not mask that as warnings.
        """
        pipeline = AISTPipeline.objects.create(
            id="local-pipe-2-ok",
            project=self.project,
            project_version=self.pv,
            status=AISTStatus.WAITING_RESULT_FROM_AI,
        )
        engagement = Engagement.objects.create(
            name="E", target_start=timezone.now(), target_end=timezone.now(),
            product=self.product,
        )
        tt = Test_Type.objects.create(name="BridgeErrTestType")
        test = Test.objects.create(
            engagement=engagement, target_start=timezone.now(),
            target_end=timezone.now(), test_type=tt,
        )
        finding = Finding.objects.create(
            test=test, title="F", severity="High",
            date=timezone.now(), reporter=self.user,
        )
        AISTAIFindingResponse.objects.create(
            pipeline=pipeline, finding=finding,
            verdict="true_positive", title="TP", summary="ok",
        )

        url = reverse("aist_api:pipeline_local_triage_complete", kwargs={"pipeline_id": pipeline.id})
        resp = self.client.post(
            url,
            data={"status": "error", "detail": "claude -p timed out after 1800s"},
            format="json",
        )

        self.assertEqual(resp.status_code, 200)
        mock_finish.assert_called_once_with("local-pipe-2-ok", degraded=False)

    def test_invalid_status_rejected(self):
        pipeline = AISTPipeline.objects.create(
            id="local-pipe-3",
            project=self.project,
            project_version=self.pv,
            status=AISTStatus.WAITING_RESULT_FROM_AI,
        )
        url = reverse("aist_api:pipeline_local_triage_complete", kwargs={"pipeline_id": pipeline.id})
        resp = self.client.post(url, data={"status": "unknown"}, format="json")
        self.assertEqual(resp.status_code, 400)

    def test_missing_pipeline_returns_404(self):
        url = reverse("aist_api:pipeline_local_triage_complete", kwargs={"pipeline_id": "nonexistent"})
        resp = self.client.post(url, data={"status": "success"}, format="json")
        self.assertEqual(resp.status_code, 404)

    @patch("aist.api.ai.finish_pipeline")
    def test_does_not_delete_existing_finding_responses(self, mock_finish):
        """Verify local triage complete does NOT call sync_ai_finding_responses."""
        pipeline = AISTPipeline.objects.create(
            id="local-pipe-4",
            project=self.project,
            project_version=self.pv,
            status=AISTStatus.WAITING_RESULT_FROM_AI,
        )
        engagement = Engagement.objects.create(
            name="E",
            target_start=timezone.now(),
            target_end=timezone.now(),
            product=self.product,
        )
        tt = Test_Type.objects.create(name="LocalTestType")
        test = Test.objects.create(
            engagement=engagement,
            target_start=timezone.now(),
            target_end=timezone.now(),
            test_type=tt,
        )
        finding = Finding.objects.create(
            test=test, title="Test", severity="High",
            date=timezone.now(), reporter=self.user,
        )
        # Pre-create a response (as if Claude skill wrote it)
        AISTAIFindingResponse.objects.create(
            pipeline=pipeline, finding=finding,
            verdict="true_positive", title="TP", summary="ok",
        )

        url = reverse("aist_api:pipeline_local_triage_complete", kwargs={"pipeline_id": pipeline.id})
        resp = self.client.post(url, data={"status": "success"}, format="json")
        self.assertEqual(resp.status_code, 200)

        # Verify the response was NOT deleted
        self.assertTrue(
            AISTAIFindingResponse.objects.filter(pipeline=pipeline, finding=finding).exists(),
        )


class PrepareAutoPushLocalTriageTests(AISTApiBase):

    """Tests for _prepare_auto_push branching between n8n and local."""

    def setUp(self):
        super().setUp()
        self.engagement = Engagement.objects.create(
            name="E", target_start=timezone.now(), target_end=timezone.now(),
            product=self.product,
        )
        self.tt = Test_Type.objects.create(name="PrepTestType")
        self.test_obj = Test.objects.create(
            engagement=self.engagement, target_start=timezone.now(),
            target_end=timezone.now(), test_type=self.tt,
        )

    def _make_pipeline_with_findings(self, *, profile=None, launch_ai=None, n_findings=3):
        if profile is not None:
            self.project.profile = profile
            self.project.save(update_fields=["profile"])
        pipeline = AISTPipeline.objects.create(
            project=self.project,
            project_version=self.pv,
            status=AISTStatus.WAITING_CONFIRMATION_TO_PUSH_TO_AI,
            launch_data={"ai": launch_ai or {}},
        )
        pipeline.tests.add(self.test_obj)
        for i in range(n_findings):
            Finding.objects.create(
                test=self.test_obj, title=f"F{i}", severity="High",
                date=timezone.now(), reporter=self.user,
            )
        return pipeline

    @patch("aist.tasks.ai.push_request_to_local_triage")
    def test_local_triage_dispatches_all_findings(self, mock_local):
        pipeline = self._make_pipeline_with_findings(
            profile={"ai_triage": {"type": "local"}},
            launch_ai={"mode": "AUTO_DEFAULT", "triage_type": "local"},
        )
        result = _prepare_auto_push(str(pipeline.id), _noop_logger())
        self.assertEqual(result, AutoPushOutcome.DISPATCHED)
        pipeline.refresh_from_db()
        self.assertEqual(pipeline.status, AISTStatus.PUSH_TO_AI)

    @patch("aist.tasks.ai.push_request_to_ai")
    def test_n8n_triage_requires_filter_snapshot(self, mock_n8n):
        pipeline = self._make_pipeline_with_findings(
            launch_ai={"mode": "AUTO_DEFAULT"},
        )
        result = _prepare_auto_push(str(pipeline.id), _noop_logger())
        self.assertEqual(result, AutoPushOutcome.NOTHING_TO_PUSH)

    @patch("aist.tasks.ai.push_request_to_local_triage")
    def test_local_triage_without_filter_takes_all(self, mock_local):
        pipeline = self._make_pipeline_with_findings(
            profile={"ai_triage": {"type": "local"}},
            launch_ai={"mode": "AUTO_DEFAULT", "triage_type": "local"},
            n_findings=5,
        )
        result = _prepare_auto_push(str(pipeline.id), _noop_logger())
        self.assertEqual(result, AutoPushOutcome.DISPATCHED)

    @patch("aist.tasks.ai.push_request_to_local_triage")
    def test_local_with_zero_findings_finishes_ok(self, mock_local):
        pipeline = self._make_pipeline_with_findings(
            profile={"ai_triage": {"type": "local"}},
            launch_ai={"mode": "AUTO_DEFAULT", "triage_type": "local"},
            n_findings=0,
        )
        result = _prepare_auto_push(str(pipeline.id), _noop_logger())
        self.assertEqual(result, AutoPushOutcome.NOTHING_TO_PUSH)
        mock_local.delay.assert_not_called()

    @patch("aist.tasks.ai.push_request_to_local_triage")
    def test_a_repeated_auto_push_leaves_a_pipeline_already_in_triage_running(self, mock_local):
        pipeline = self._make_pipeline_with_findings(
            launch_ai={"mode": "AUTO_DEFAULT", "triage_type": "local"},
        )
        AISTPipeline.objects.filter(pk=pipeline.pk).update(status=AISTStatus.WAITING_RESULT_FROM_AI)

        auto_push_to_ai_if_configured.run(str(pipeline.id))

        pipeline.refresh_from_db()
        self.assertEqual(pipeline.status, AISTStatus.WAITING_RESULT_FROM_AI)
        mock_local.delay.assert_not_called()

    @patch("aist.tasks.ai.push_request_to_local_triage")
    def test_a_filterless_auto_launch_is_triaged_locally_after_enrichment_when_the_profile_says_local(self, mock_local):
        pipeline = self._make_pipeline_with_findings(
            profile={"ai_triage": {"type": "local"}},
            launch_ai={"mode": "AUTO_DEFAULT"},
        )
        AISTPipeline.objects.filter(pk=pipeline.pk).update(status=AISTStatus.FINDING_POSTPROCESSING)
        finding_ids = sorted(Finding.objects.filter(test=self.test_obj).values_list("id", flat=True))

        with (
            patch("aist.tasks.ai.auto_push_to_ai_if_configured.delay", side_effect=auto_push_to_ai_if_configured.run),
            self.captureOnCommitCallbacks(execute=True),
        ):
            after_upload_enrich_and_watch.run([len(finding_ids)], str(pipeline.id), [self.test_obj.id], "INFO")

        pipeline.refresh_from_db()
        self.assertEqual(pipeline.status, AISTStatus.PUSH_TO_AI)
        mock_local.delay.assert_called_once()
        self.assertEqual(mock_local.delay.call_args.args[0], str(pipeline.id))
        self.assertEqual(sorted(mock_local.delay.call_args.args[1]), finding_ids)


class PushLocalTriageTaskTests(AISTApiBase):

    """Tests for push_request_to_local_triage Celery task."""

    def setUp(self):
        super().setUp()

        from aist.models import (  # noqa: PLC0415
            Organization,
            OrgIntegration,
            OrgIntegrationType,
        )

        # The triage flow resolves Claude credentials per project; for the
        # bridge to be invoked, the project's org must have an active
        # CLAUDE_CODE OrgIntegration. Mirrors the setup in
        # ``test_claude_analyze.py``.
        self.org_prod_type = self.prod_type
        self.org = Organization.objects.create(
            name="Local Triage Org",
            product_type=self.org_prod_type,
        )
        self.project.refresh_from_db()
        self.claude_integration = OrgIntegration.objects.create(
            organization=self.org,
            integration_type=OrgIntegrationType.CLAUDE_CODE,
            name="primary",
            secret="sk-ant-oat01-local-triage-test-token-12345",  # noqa: S106
            is_active=True,
            config={"auth_mode": "oauth"},
        )

    @patch("aist.tasks.ai.build_bridge_client_from_settings")
    def test_dispatches_to_bridge_via_uds(self, mock_bridge_factory):
        pipeline = AISTPipeline.objects.create(
            id="push-local-1",
            project=self.project,
            project_version=self.pv,
            status=AISTStatus.PUSH_TO_AI,
            launch_data={"project_path": "/tmp/aist/projects/test"},  # noqa: S108
        )
        mock_client = MagicMock()
        mock_bridge_factory.return_value = mock_client

        push_request_to_local_triage(str(pipeline.id), [1, 2, 3])

        mock_bridge_factory.assert_called_once()
        # Factory must receive Claude credentials resolved from the
        # project's org integration.
        factory_kwargs = mock_bridge_factory.call_args.kwargs
        self.assertEqual(
            factory_kwargs.get("auth_env"),
            {"CLAUDE_CODE_OAUTH_TOKEN": "sk-ant-oat01-local-triage-test-token-12345"},
        )
        mock_client.analyze_async.assert_called_once()
        call_kwargs = mock_client.analyze_async.call_args.kwargs
        self.assertEqual(call_kwargs["project_id"], str(pipeline.id))
        self.assertEqual(call_kwargs["skill_name"], "aist-finding-triage")
        self.assertEqual(call_kwargs["source_path"], "/tmp/aist/projects/test")  # noqa: S108
        self.assertIn(str(pipeline.id), call_kwargs["callback_url"])
        # Completion-marker convention — without these the bridge can only
        # detect completion via the (3h) AIST_LOCAL_TRIAGE_TIMEOUT.
        self.assertIn("output_path=", call_kwargs["extra_args"])
        self.assertIn("result_filename=triage_done.flag", call_kwargs["extra_args"])
        self.assertIn(str(pipeline.id), call_kwargs["extra_args"])  # per-pipeline subdir

        pipeline.refresh_from_db()
        self.assertEqual(pipeline.status, AISTStatus.WAITING_RESULT_FROM_AI)

    @patch("aist.tasks.ai.finish_pipeline")
    @patch("aist.tasks.ai.build_bridge_client_from_settings")
    def test_bridge_unreachable_finishes_degraded(self, mock_bridge_factory, mock_finish):
        pipeline = AISTPipeline.objects.create(
            id="push-local-2",
            project=self.project,
            project_version=self.pv,
            status=AISTStatus.PUSH_TO_AI,
            launch_data={"project_path": "/tmp/aist/projects/test"},  # noqa: S108
        )
        mock_client = MagicMock()
        mock_client.analyze_async.side_effect = Exception("socket not found")
        mock_bridge_factory.return_value = mock_client

        push_request_to_local_triage(str(pipeline.id), [1])

        mock_finish.assert_called_once_with(str(pipeline.id), degraded=True)

    @patch("aist.tasks.ai.finish_pipeline")
    @patch("aist.tasks.ai.build_bridge_client_from_settings")
    def test_missing_claude_integration_skips_bridge_and_finishes_degraded(
        self, mock_bridge_factory, mock_finish,
    ):
        # Deactivate the integration set up by setUp() — the triage flow
        # must short-circuit BEFORE building the bridge client.
        self.claude_integration.is_active = False
        self.claude_integration.save(update_fields=["is_active"])

        pipeline = AISTPipeline.objects.create(
            id="push-local-3",
            project=self.project,
            project_version=self.pv,
            status=AISTStatus.PUSH_TO_AI,
            launch_data={"project_path": "/tmp/aist/projects/test"},  # noqa: S108
        )

        push_request_to_local_triage(str(pipeline.id), [1, 2, 3])

        mock_bridge_factory.assert_not_called()
        mock_finish.assert_called_once_with(str(pipeline.id), degraded=True)


class SendRequestRoutingTests(AISTApiBase):

    """Tests for send_request_to_ai_for_pipeline triage type routing."""

    def setUp(self):
        super().setUp()
        self.login_as_superuser()
        self.engagement = Engagement.objects.create(
            name="E", target_start=timezone.now(), target_end=timezone.now(),
            product=self.product,
        )
        self.tt = Test_Type.objects.create(name="RouteTestType")
        self.test_obj = Test.objects.create(
            engagement=self.engagement, target_start=timezone.now(),
            target_end=timezone.now(), test_type=self.tt,
        )

    @patch("aist.tasks.ai.push_request_to_local_triage")
    @patch("aist.tasks.ai.push_request_to_ai")
    @patch("aist.api.ai.install_pipeline_logging")
    def test_local_project_routes_to_local_task(self, mock_log, mock_n8n, mock_local):
        mock_log.return_value = _noop_logger()
        self.project.profile = {"ai_triage": {"type": "local"}}
        self.project.save(update_fields=["profile"])

        pipeline = AISTPipeline.objects.create(
            id="route-local-1",
            project=self.project,
            project_version=self.pv,
            status=AISTStatus.WAITING_CONFIRMATION_TO_PUSH_TO_AI,
        )
        finding = Finding.objects.create(
            test=self.test_obj, title="F1", severity="High",
            date=timezone.now(), reporter=self.user,
        )

        url = reverse("aist_api:pipeline_send_request", kwargs={"pipeline_id": pipeline.id})
        resp = self.client.post(
            url,
            data=json.dumps({"finding_ids": [finding.id], "filters": {}}),
            content_type="application/json",
        )

        self.assertEqual(resp.status_code, 200)
        mock_local.delay.assert_called_once()
        mock_n8n.delay.assert_not_called()

    @patch("aist.tasks.ai.push_request_to_local_triage")
    @patch("aist.tasks.ai.push_request_to_ai")
    @patch("aist.api.ai.install_pipeline_logging")
    def test_n8n_project_routes_to_n8n_task(self, mock_log, mock_n8n, mock_local):
        mock_log.return_value = _noop_logger()

        pipeline = AISTPipeline.objects.create(
            id="route-n8n-1",
            project=self.project,
            project_version=self.pv,
            status=AISTStatus.WAITING_CONFIRMATION_TO_PUSH_TO_AI,
        )
        finding = Finding.objects.create(
            test=self.test_obj, title="F2", severity="High",
            date=timezone.now(), reporter=self.user,
        )

        url = reverse("aist_api:pipeline_send_request", kwargs={"pipeline_id": pipeline.id})
        resp = self.client.post(
            url,
            data=json.dumps({"finding_ids": [finding.id], "filters": {}}),
            content_type="application/json",
        )

        self.assertEqual(resp.status_code, 200)
        mock_n8n.delay.assert_called_once()
        mock_local.delay.assert_not_called()


class PipelineArgsTriageTypeTests(AISTApiBase):

    """Tests for ai_triage_type in PipelineArguments."""

    def test_normalize_accepts_valid_triage_types(self):
        for triage_type in ("n8n", "local"):
            params = {"project_id": self.project.id, "ai_triage_type": triage_type}
            normalized = PipelineArguments.normalize_params(project=self.project, raw_params=params)
            self.assertEqual(normalized["ai_triage_type"], triage_type)

    def test_a_launch_choosing_no_triage_type_is_fixed_to_the_project_triage_at_admission(self):
        self.project.profile = {"ai_triage": {"type": "local"}}
        self.project.save(update_fields=["profile"])

        normalized = PipelineArguments.normalize_params(project=self.project, raw_params={"ai_mode": "AUTO_DEFAULT"})

        self.assertEqual(normalized["ai_triage_type"], "local")

    def test_normalize_rejects_invalid_triage_type(self):
        params = {"project_id": self.project.id, "ai_triage_type": "invalid"}
        with self.assertRaises(ValueError):
            PipelineArguments.normalize_params(project=self.project, raw_params=params)

    def test_auto_default_local_without_snapshot_is_ok(self):
        params = {
            "project_id": self.project.id,
            "ai_mode": "AUTO_DEFAULT",
            "ai_triage_type": "local",
        }
        normalized = PipelineArguments.normalize_params(project=self.project, raw_params=params)
        self.assertIsNone(normalized["ai_filter_snapshot"])
        self.assertEqual(normalized["ai_triage_type"], "local")

    def test_auto_default_n8n_without_snapshot_fails(self):
        params = {
            "project_id": self.project.id,
            "ai_mode": "AUTO_DEFAULT",
            "ai_triage_type": "n8n",
        }
        with self.assertRaisesMessage(ValueError, "ai_filter is required for AUTO_DEFAULT"):
            PipelineArguments.normalize_params(project=self.project, raw_params=params)

    def test_auto_default_without_snapshot_is_ok_when_the_project_triages_locally(self):
        self.project.profile = {"ai_triage": {"type": "local"}}
        self.project.save(update_fields=["profile"])

        normalized = PipelineArguments.normalize_params(
            project=self.project,
            raw_params={"project_id": self.project.id, "ai_mode": "AUTO_DEFAULT"},
        )

        self.assertEqual(normalized["ai_mode"], "AUTO_DEFAULT")
        self.assertIsNone(normalized["ai_filter_snapshot"])


class SastRunRecordsLaunchAISettingsTests(SastWorkerTestMixin, AISTApiBase):

    """A SAST run stores the launch's AI settings, which decide whether its findings later reach triage on their own."""

    def test_an_auto_default_launch_with_local_triage_is_recorded_and_pushes_without_a_filter(self):
        pipeline = AISTPipeline.objects.create(
            id="pipe-ai-settings", project=self.project, project_version=self.pv, status=AISTStatus.FINISHED,
        )

        self.run_sast(
            pipeline,
            project_version=self.pv,
            upload_results=[],
            launch_params={"ai_mode": "AUTO_DEFAULT", "ai_triage_type": "local"},
        )

        pipeline.refresh_from_db()
        self.assertEqual(
            pipeline.launch_data["ai"], {"mode": "AUTO_DEFAULT", "triage_type": "local", "filter_snapshot": None},
        )
        self.assertTrue(PipelineLaunchData(pipeline.launch_data).ai.pushes_automatically(resolve_triage_type(pipeline)))

    def test_a_launch_admitted_under_local_triage_still_pushes_after_the_profile_switches_to_n8n(self):
        self.project.profile = {"ai_triage": {"type": "local"}}
        self.project.save(update_fields=["profile"])
        admitted = PipelineArguments.normalize_params(project=self.project, raw_params={"ai_mode": "AUTO_DEFAULT"})
        self.project.profile = {"ai_triage": {"type": "n8n"}}
        self.project.save(update_fields=["profile"])
        pipeline = AISTPipeline.objects.create(
            id="pipe-ai-frozen-triage", project=self.project, project_version=self.pv, status=AISTStatus.FINISHED,
        )

        self.run_sast(pipeline, project_version=self.pv, upload_results=[], launch_params=admitted)

        pipeline.refresh_from_db()
        self.assertEqual(resolve_triage_type(pipeline), "local")
        self.assertTrue(PipelineLaunchData(pipeline.launch_data).ai.pushes_automatically(resolve_triage_type(pipeline)))
