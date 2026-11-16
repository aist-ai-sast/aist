from __future__ import annotations

import io
import json
import tempfile
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from celery import current_app, signature
from celery.exceptions import Ignore
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone
from dojo.models import Engagement, Finding, Test, Test_Type
from kombu.exceptions import OperationalError

from aist.launch_data import PipelineLaunchData
from aist.models import (
    AISTAIFindingResponse,
    AISTAIResponse,
    AISTPipeline,
    AISTStatus,
    LocalTriageBatch,
    LocalTriageBatchState,
    Organization,
)
from aist.pipeline_args import PipelineArguments
from aist.profile import ProjectProfile
from aist.services.ai_triage import resolve_triage_type
from aist.tasks.ai import AutoPushOutcome, _prepare_auto_push, auto_push_to_ai_if_configured
from aist.tasks.enrich import after_upload_enrich_and_watch
from aist.tasks.local_triage import push_request_to_local_triage, triage_local_batch
from aist.test.pipeline_execution_helpers import (
    SastWorkerTestMixin,
    make_ai_finding_response,
    make_claude_integration,
    make_test_with_finding,
)
from aist.test.test_api import AISTApiBase
from aist.utils.bridge_client_factory import BridgeSessionResult
from aist.utils.pipeline import finish_pipeline, stop_pipeline


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


class LocalTriageFixtures:

    """A project triaged locally with an active Claude integration, and its pipelines' findings, batches and answers."""

    def _use_local_triage(self):
        organization = Organization.objects.create(name="Local Triage Org", product_type=self.prod_type)
        self.claude_integration = make_claude_integration(
            organization,
            secret="sk-ant-oat01-local-triage-test-token-12345",  # noqa: S106
        )
        self.project.profile = {"ai_triage": {"type": "local"}}
        self.project.save(update_fields=["profile"])
        self.project.refresh_from_db()

    def _pipeline_findings(self, pipeline, count: int) -> list[Finding]:
        findings = []
        for index in range(count):
            test, finding = make_test_with_finding(self.product, self.user, file_path=f"src/module_{index}.py")
            pipeline.tests.add(test)
            findings.append(finding)
        return findings

    def _batch(self, pipeline, findings, *, sequence=1, state=LocalTriageBatchState.PENDING):
        return LocalTriageBatch.objects.create(
            pipeline=pipeline, sequence=sequence, finding_ids=[finding.id for finding in findings], state=state,
        )

    def _answer(self, pipeline, *findings):
        for finding in findings:
            make_ai_finding_response(pipeline, finding, title="TP", summary="ok")

    def _push_to_local_triage(self, pipeline, findings, *args):
        """Deliver the push task, then run what it queued after its commit."""
        with self.captureOnCommitCallbacks(execute=True):
            push_request_to_local_triage(pipeline.id, [finding.id for finding in findings], *args)

    @staticmethod
    def _status(pipeline) -> str:
        pipeline.refresh_from_db()
        return pipeline.status

    def _states(self, pipeline):
        return list(LocalTriageBatch.objects.filter(pipeline=pipeline).order_by("sequence").values_list("state", flat=True))


class LocalTriageRuntimeFixtures(LocalTriageFixtures):

    """A bridge whose sessions answer the findings they are given, and Celery tasks that run in this process."""

    def _use_triage_runtime(self):
        self._use_local_triage()
        self.unanswered_by_session: set[int] = set()
        self.session_result = BridgeSessionResult(status="success")
        self.sessions: list[dict] = []
        self.bridge = MagicMock()
        self.bridge.analyze_sync.side_effect = self._session
        self.bridge_factory = self.enterContext(
            patch("aist.tasks.local_triage.build_bridge_client_from_settings", return_value=self.bridge),
        )
        self.output_root = self.enterContext(tempfile.TemporaryDirectory())
        self.enterContext(override_settings(
            CELERY_TASK_ALWAYS_EAGER=True, CELERY_TASK_EAGER_PROPAGATES=True, AIST_TRIAGE_OUTPUT_DIR=self.output_root,
        ))

    def _session(self, **request):
        """One skill session: it writes a verdict for every finding it was given except ``unanswered_by_session``."""
        self.sessions.append(request)
        pipeline = AISTPipeline.objects.get(pk=request["project_id"])
        for finding in Finding.objects.filter(id__in=self._session_finding_ids(request)):
            if finding.id not in self.unanswered_by_session:
                self._answer(pipeline, finding)
        return self.session_result

    @staticmethod
    def _session_args(session: dict) -> dict[str, str]:
        return dict(token.split("=", 1) for token in session["extra_args"].split())

    def _session_finding_ids(self, session: dict) -> list[int]:
        return [int(value) for value in self._session_args(session)["finding_ids"].split(",")]


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


@override_settings(AIST_LOCAL_TRIAGE_BATCH_SIZE=2)
class LocalTriageFlowTests(LocalTriageRuntimeFixtures, AISTApiBase):

    """A pipeline's findings are triaged one bounded batch per bridge session, then its triage is finished."""

    def setUp(self):
        super().setUp()
        self._use_triage_runtime()
        self.pipeline = self.make_pipeline(
            "flow-1", status=AISTStatus.PUSH_TO_AI, launch_data={"project_path": "/tmp/aist/projects/test"},  # noqa: S108
        )

    def _push(self, findings):
        self._push_to_local_triage(self.pipeline, findings)

    def test_a_large_selection_is_triaged_one_bounded_batch_per_session_and_then_finished(self):
        findings = self._pipeline_findings(self.pipeline, 3)

        self._push(reversed(findings))

        self.assertEqual(
            [self._session_finding_ids(session) for session in self.sessions],
            [[findings[0].id, findings[1].id], [findings[2].id]],
        )
        first, second = self.sessions
        self.assertEqual(first["skill_name"], "aist-finding-triage")
        self.assertEqual(first["project_id"], self.pipeline.id)
        self.assertEqual(first["source_path"], "/tmp/aist/projects/test")  # noqa: S108
        self.assertEqual(self._session_args(first)["result_filename"], "triage_done.flag")
        self.assertNotEqual(self._session_args(first)["output_path"], self._session_args(second)["output_path"])
        self.assertEqual(
            self.bridge_factory.call_args.kwargs["auth_env"],
            {"CLAUDE_CODE_OAUTH_TOKEN": "sk-ant-oat01-local-triage-test-token-12345"},
        )
        self.assertEqual(self._states(self.pipeline), [LocalTriageBatchState.COMPLETED] * 2)
        self.assertEqual(self._status(self.pipeline), AISTStatus.FINISHED)

    def test_findings_outside_the_pipeline_never_reach_a_session(self):
        (own,) = self._pipeline_findings(self.pipeline, 1)
        _, other_organization_finding = make_test_with_finding(self.other_product, self.user)
        _, earlier_pipeline_finding = make_test_with_finding(self.product, self.user)

        self._push([other_organization_finding, own, earlier_pipeline_finding])

        self.assertEqual([self._session_finding_ids(session) for session in self.sessions], [[own.id]])

    def test_a_finding_an_analyzer_already_judged_keeps_its_verdict(self):
        judged, open_finding = self._pipeline_findings(self.pipeline, 2)
        analyzer_response = AISTAIResponse.objects.create(
            pipeline=self.pipeline, payload={"results": {}}, source=AISTAIResponse.Source.AGENT_ANALYZER,
        )
        AISTAIFindingResponse.objects.create(
            pipeline=self.pipeline, source_response=analyzer_response, finding=judged,
            verdict=AISTAIFindingResponse.Verdict.FALSE_POSITIVE, title="analyzer verdict",
        )

        self._push([judged, open_finding])

        self.assertEqual([self._session_finding_ids(session) for session in self.sessions], [[open_finding.id]])
        self.assertEqual(
            AISTAIFindingResponse.objects.get(pipeline=self.pipeline, finding=judged).title, "analyzer verdict",
        )

    def test_a_selection_whose_findings_all_have_a_verdict_finishes_without_warnings(self):
        answered, closed = self._pipeline_findings(self.pipeline, 2)
        self._answer(self.pipeline, answered)
        closed.active = False
        closed.save(update_fields=["active"])

        self._push([answered, closed])

        self.assertEqual(self.sessions, [])
        self.assertEqual(self._states(self.pipeline), [])
        self.assertEqual(self._status(self.pipeline), AISTStatus.FINISHED)

    def test_a_selection_with_none_of_the_pipeline_findings_finishes_with_warnings(self):
        _, earlier_pipeline_finding = make_test_with_finding(self.product, self.user)

        self._push([earlier_pipeline_finding])

        self.assertEqual(self.sessions, [])
        self.assertEqual(self._states(self.pipeline), [])
        self.assertEqual(self._status(self.pipeline), AISTStatus.FINISHED_WITH_WARNINGS)

    def test_a_finding_a_session_left_unanswered_makes_the_pipeline_finish_with_warnings(self):
        """A session that reports success but answers only part of its batch must not look complete."""
        findings = self._pipeline_findings(self.pipeline, 3)
        self.unanswered_by_session = {findings[1].id}

        self._push(findings)

        self.assertEqual(len(self.sessions), 2)
        self.assertEqual(self._status(self.pipeline), AISTStatus.FINISHED_WITH_WARNINGS)

    def test_a_finding_answered_only_under_another_pipeline_is_triaged_here(self):
        (finding,) = self._pipeline_findings(self.pipeline, 1)
        self._answer(self.make_pipeline("flow-other", status=AISTStatus.FINISHED), finding)

        self._push([finding])

        self.assertEqual([self._session_finding_ids(session) for session in self.sessions], [[finding.id]])
        self.assertEqual(self._status(self.pipeline), AISTStatus.FINISHED)

    def test_a_session_error_after_every_finding_was_answered_finishes_cleanly(self):
        """The bridge can time out after the session already wrote every verdict; nothing is missing then."""
        self.session_result = BridgeSessionResult(status="error", detail="claude -p timed out")

        self._push(self._pipeline_findings(self.pipeline, 1))

        self.assertEqual(self._status(self.pipeline), AISTStatus.FINISHED)

    def test_a_batch_whose_session_fails_does_not_stop_the_batches_after_it(self):
        findings = self._pipeline_findings(self.pipeline, 3)
        answer_and_return = self.bridge.analyze_sync.side_effect

        def first_fails_then_answers(**request):
            if not self.sessions:
                self.sessions.append(request)
                msg = "bridge socket vanished"
                raise RuntimeError(msg)
            return answer_and_return(**request)

        self.bridge.analyze_sync.side_effect = first_fails_then_answers

        self._push(findings)

        self.assertEqual(self._session_finding_ids(self.sessions[1]), [findings[2].id])
        self.assertEqual(self._states(self.pipeline), [LocalTriageBatchState.COMPLETED] * 2)
        self.assertEqual(self._status(self.pipeline), AISTStatus.FINISHED_WITH_WARNINGS)

    def test_a_deactivated_claude_integration_triages_nothing_and_finishes_with_warnings(self):
        self.claude_integration.is_active = False
        self.claude_integration.save(update_fields=["is_active"])

        self._push(self._pipeline_findings(self.pipeline, 3))

        self.bridge_factory.assert_not_called()
        self.assertEqual(self._status(self.pipeline), AISTStatus.FINISHED_WITH_WARNINGS)

    def test_a_repeated_push_while_the_pipeline_waits_for_its_triage_changes_nothing(self):
        findings = self._pipeline_findings(self.pipeline, 2)
        AISTPipeline.objects.filter(pk=self.pipeline.pk).update(status=AISTStatus.WAITING_RESULT_FROM_AI)
        self._batch(self.pipeline, findings)

        self._push(findings)

        self.assertEqual(self.sessions, [])
        self.assertEqual(self._states(self.pipeline), [LocalTriageBatchState.PENDING])
        self.assertEqual(self._status(self.pipeline), AISTStatus.WAITING_RESULT_FROM_AI)

    def test_a_push_to_a_finished_pipeline_changes_nothing(self):
        findings = self._pipeline_findings(self.pipeline, 3)
        self._push(findings)

        self._push(findings)

        self.assertEqual(len(self.sessions), 2)
        self.assertEqual(len(self._states(self.pipeline)), 2)
        self.assertEqual(self._status(self.pipeline), AISTStatus.FINISHED)

    def test_a_second_delivery_of_a_completed_batch_runs_no_session_and_ends_its_chain(self):
        findings = self._pipeline_findings(self.pipeline, 3)
        self._push(findings)
        first_batch = LocalTriageBatch.objects.filter(pipeline=self.pipeline).order_by("sequence").first()

        # Celery's Ignore: the worker acknowledges the message and sends no next task of the chain.
        with self.assertRaises(Ignore):
            triage_local_batch(self.pipeline.id, first_batch.pk)

        self.assertEqual(len(self.sessions), 2)
        self.assertEqual(self._states(self.pipeline), [LocalTriageBatchState.COMPLETED] * 2)
        self.assertEqual(self._status(self.pipeline), AISTStatus.FINISHED)

    def test_a_batch_redelivered_after_its_worker_was_lost_triages_only_what_is_still_unanswered(self):
        AISTPipeline.objects.filter(pk=self.pipeline.pk).update(status=AISTStatus.WAITING_RESULT_FROM_AI)
        answered, unanswered = self._pipeline_findings(self.pipeline, 2)
        batch = self._batch(self.pipeline, [answered, unanswered], state=LocalTriageBatchState.RUNNING)
        self._answer(self.pipeline, answered)

        triage_local_batch(self.pipeline.id, batch.pk)

        self.assertEqual([self._session_finding_ids(session) for session in self.sessions], [[unanswered.id]])
        self.assertEqual(self._states(self.pipeline), [LocalTriageBatchState.COMPLETED])


@override_settings(AIST_LOCAL_TRIAGE_BATCH_SIZE=1, CELERY_TASK_ALWAYS_EAGER=False)
class LocalTriageDispatchTests(LocalTriageFixtures, AISTApiBase):

    """The triage chain as a worker sends it: through DefectDojo's task dispatch, up to the broker."""

    def test_every_task_of_the_chain_accepts_the_message_the_dispatch_sends(self):
        self._use_local_triage()
        pipeline = self.make_pipeline("dispatch-1", status=AISTStatus.PUSH_TO_AI)
        findings = self._pipeline_findings(pipeline, 2)
        send_task = self.enterContext(patch.object(current_app, "send_task"))

        self._push_to_local_triage(pipeline, findings, "DEBUG")
        # A worker sends the next task of the chain once the previous one has ended.
        while chained := send_task.call_args.kwargs.get("chain"):
            signature(chained[-1], app=current_app).apply_async(chain=chained[:-1])

        sent = [(call.args[0], list(call.args[1])) for call in send_task.call_args_list]
        batches = LocalTriageBatch.objects.filter(pipeline=pipeline).order_by("sequence")
        self.assertEqual([name for name, _ in sent], ["aist.triage_local_batch"] * 2 + ["aist.finish_local_triage"])
        self.assertEqual([args for _, args in sent], [
            [pipeline.id, batches[0].pk, "DEBUG"], [pipeline.id, batches[1].pk, "DEBUG"], [pipeline.id, "DEBUG"],
        ])
        self.assertEqual(self._status(pipeline), AISTStatus.WAITING_RESULT_FROM_AI)

    def test_a_triage_the_broker_does_not_accept_finishes_the_pipeline_with_warnings(self):
        self._use_local_triage()
        pipeline = self.make_pipeline("dispatch-down", status=AISTStatus.PUSH_TO_AI)
        findings = self._pipeline_findings(pipeline, 2)
        self.enterContext(patch.object(current_app, "send_task", side_effect=OperationalError("broker down")))

        self._push_to_local_triage(pipeline, findings)

        self.assertEqual(self._states(pipeline), [LocalTriageBatchState.CANCELLED] * 2)
        self.assertEqual(self._status(pipeline), AISTStatus.FINISHED_WITH_WARNINGS)


class StopPipelineCancelsLocalTriageTests(LocalTriageRuntimeFixtures, AISTApiBase):

    def setUp(self):
        super().setUp()
        self._use_triage_runtime()
        self.enterContext(patch("aist.utils.pipeline.cleanup_pipeline_containers"))

    def _delete(self, pipeline):
        return self.client.delete(reverse("aist_api:pipeline_status", kwargs={"pipeline_id": pipeline.id}))

    def test_a_stop_keeps_the_sources_of_a_running_session_until_its_worker_reports_back(self):
        pipeline = self.make_pipeline("stop-local", status=AISTStatus.WAITING_RESULT_FROM_AI)
        first, second = self._pipeline_findings(pipeline, 2)
        running = self._batch(pipeline, [first], state=LocalTriageBatchState.RUNNING)
        self._batch(pipeline, [second], sequence=2)

        stop_pipeline(pipeline)

        self.assertEqual(self._states(pipeline), [LocalTriageBatchState.CANCELLING, LocalTriageBatchState.CANCELLED])
        self.assertIn(self._status(pipeline), {AISTStatus.FINISHED, AISTStatus.FINISHED_WITH_WARNINGS})
        self.assertEqual(self._delete(pipeline).status_code, 400)
        # The session's worker was lost; the broker redelivers its task, which runs no session.
        with self.assertRaises(Ignore):
            triage_local_batch(pipeline.id, running.pk)
        self.assertEqual(self.sessions, [])
        self.assertEqual(self._states(pipeline), [LocalTriageBatchState.CANCELLED] * 2)
        self.assertEqual(self._delete(pipeline).status_code, 204)

    def test_a_pipeline_finished_by_its_own_flow_cancels_its_open_batches(self):
        pipeline = self.make_pipeline("finish-local", status=AISTStatus.WAITING_RESULT_FROM_AI)
        first, second = self._pipeline_findings(pipeline, 2)
        self._batch(pipeline, [first], state=LocalTriageBatchState.RUNNING)
        self._batch(pipeline, [second], sequence=2)

        finish_pipeline(pipeline.id)

        self.assertEqual(self._states(pipeline), [LocalTriageBatchState.CANCELLING, LocalTriageBatchState.CANCELLED])

    @override_settings(AIST_LOCAL_TRIAGE_BATCH_SIZE=1)
    def test_a_stop_during_a_session_skips_the_batches_after_it(self):
        pipeline = self.make_pipeline(
            "stop-mid", status=AISTStatus.PUSH_TO_AI, launch_data={"project_path": "/tmp/aist/projects/test"},  # noqa: S108
        )
        findings = self._pipeline_findings(pipeline, 3)
        answer_and_return = self.bridge.analyze_sync.side_effect

        deletes_during_the_session = []

        def session_during_which_the_user_stops(**request):
            stop_pipeline(AISTPipeline.objects.get(pk=pipeline.id))
            deletes_during_the_session.append(self._delete(pipeline).status_code)
            return answer_and_return(**request)

        self.bridge.analyze_sync.side_effect = session_during_which_the_user_stops

        self._push_to_local_triage(pipeline, findings)

        self.assertEqual(len(self.sessions), 1)
        self.assertEqual(deletes_during_the_session, [400])
        self.assertEqual(self._states(pipeline), [LocalTriageBatchState.CANCELLED] * 3)
        self.assertEqual(self._delete(pipeline).status_code, 204)

    def test_a_triage_push_that_lands_while_the_pipeline_is_being_stopped_is_cancelled(self):
        pipeline = self.make_pipeline(
            "stop-push", status=AISTStatus.PUSH_TO_AI, launch_data={"project_path": "/tmp/aist/projects/test"},  # noqa: S108
        )
        findings = self._pipeline_findings(pipeline, 2)

        def push_lands_before_the_terminal_status(**_kwargs):
            push_request_to_local_triage(pipeline.id, [finding.id for finding in findings])
            return SimpleNamespace(remaining_violations=0)

        with (
            patch("aist.utils.pipeline.reconcile_pipeline_orphans", side_effect=push_lands_before_the_terminal_status),
            self.captureOnCommitCallbacks(execute=True),
        ):
            stop_pipeline(pipeline)

        self.assertEqual(self.sessions, [])
        self.assertEqual(self._states(pipeline), [LocalTriageBatchState.CANCELLED])


@override_settings(AIST_LOCAL_TRIAGE_BATCH_SIZE=10)
class RetriagePipelineCommandTests(LocalTriageRuntimeFixtures, AISTApiBase):

    """retriage_pipeline triages a finished pipeline's findings that hold no AI response for it, in the same batches."""

    def setUp(self):
        super().setUp()
        self._use_triage_runtime()
        self.sources = self.enterContext(tempfile.TemporaryDirectory())

    def _pipeline(self, pipeline_id, status=AISTStatus.FINISHED_WITH_WARNINGS, project_path=None):
        pipeline = self.make_pipeline(
            pipeline_id, status=status, launch_data={"project_path": project_path or self.sources},
        )
        answered, closed, untriaged, answered_elsewhere = self._pipeline_findings(pipeline, 4)
        closed.active = False
        closed.save(update_fields=["active"])
        self._answer(pipeline, answered)
        self._answer(self.make_pipeline(f"{pipeline_id}-other", status=AISTStatus.FINISHED), answered_elsewhere)
        return pipeline, [untriaged, answered_elsewhere]

    def _retriage(self, pipeline):
        stdout = io.StringIO()
        call_command("retriage_pipeline", pipeline.id, stdout=stdout)
        return stdout.getvalue()

    def test_active_findings_without_an_ai_response_for_the_pipeline_are_triaged_and_its_status_kept(self):
        pipeline, untriaged = self._pipeline("retriage-done")

        output = self._retriage(pipeline)

        self.assertEqual(
            [self._session_finding_ids(session) for session in self.sessions],
            [sorted(finding.id for finding in untriaged)],
        )
        self.assertIn("queued 2 findings in 1 batches", output)
        self.assertEqual(self._states(pipeline), [LocalTriageBatchState.COMPLETED])
        self.assertEqual(self._status(pipeline), AISTStatus.FINISHED_WITH_WARNINGS)

    @override_settings(AIST_LOCAL_TRIAGE_BATCH_SIZE=1)
    def test_a_retriage_the_user_stops_runs_no_further_session_and_releases_the_pipeline(self):
        self.enterContext(patch("aist.utils.pipeline.cleanup_pipeline_containers"))
        pipeline, _ = self._pipeline("retriage-stop")
        answer_and_return = self.bridge.analyze_sync.side_effect

        def session_during_which_the_user_stops(**request):
            stop_pipeline(AISTPipeline.objects.get(pk=pipeline.id))
            return answer_and_return(**request)

        self.bridge.analyze_sync.side_effect = session_during_which_the_user_stops

        self._retriage(pipeline)

        self.assertEqual(len(self.sessions), 1)
        self.assertEqual(self._states(pipeline), [LocalTriageBatchState.CANCELLED] * 2)
        self.assertEqual(self._status(pipeline), AISTStatus.FINISHED_WITH_WARNINGS)
        url = reverse("aist_api:pipeline_status", kwargs={"pipeline_id": pipeline.id})
        self.assertEqual(self.client.delete(url).status_code, 204)

    def test_a_running_pipeline_is_left_to_its_own_triage(self):
        pipeline, _ = self._pipeline("retriage-running", status=AISTStatus.WAITING_RESULT_FROM_AI)

        with self.assertRaisesMessage(CommandError, "its own run triages"):
            self._retriage(pipeline)

        self.assertEqual(self._states(pipeline), [])

    def test_a_pipeline_triaged_through_the_webhook_is_refused(self):
        pipeline, _ = self._pipeline("retriage-n8n")
        self.project.profile = {"ai_triage": {"type": "n8n"}}
        self.project.save(update_fields=["profile"])

        with self.assertRaisesMessage(CommandError, "is not triaged locally"):
            self._retriage(pipeline)

        self.assertEqual(self._states(pipeline), [])

    def test_a_retriage_already_in_progress_is_not_queued_twice(self):
        pipeline, untriaged = self._pipeline("retriage-twice")
        self._batch(pipeline, untriaged)

        with self.assertRaisesMessage(CommandError, "already has triage batches"):
            self._retriage(pipeline)

        self.assertEqual(self._states(pipeline), [LocalTriageBatchState.PENDING])
        self.assertEqual(self.sessions, [])

    def test_a_retriage_the_broker_does_not_accept_can_be_queued_again(self):
        pipeline, untriaged = self._pipeline("retriage-broker")

        with (
            override_settings(CELERY_TASK_ALWAYS_EAGER=False),
            patch.object(current_app, "send_task", side_effect=OperationalError("broker down")),
            self.assertRaises(OperationalError),
        ):
            self._retriage(pipeline)

        self.assertEqual(self._states(pipeline), [LocalTriageBatchState.CANCELLED])
        self.assertIn("queued 2 findings in 1 batches", self._retriage(pipeline))
        self.assertEqual(
            [self._session_finding_ids(session) for session in self.sessions],
            [sorted(finding.id for finding in untriaged)],
        )
        self.assertEqual(self._status(pipeline), AISTStatus.FINISHED_WITH_WARNINGS)

    def test_a_pipeline_whose_sources_are_gone_is_refused(self):
        pipeline, _ = self._pipeline("retriage-nosrc", project_path=f"{self.sources}/removed")

        with self.assertRaisesMessage(CommandError, "are not at"):
            self._retriage(pipeline)

        self.assertEqual(self.sessions, [])
        self.assertEqual(self._states(pipeline), [LocalTriageBatchState.CANCELLED])


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
    def test_local_project_routes_to_local_task(self, mock_n8n, mock_local):
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
        with self.captureOnCommitCallbacks(execute=True):
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
    def test_n8n_project_routes_to_n8n_task(self, mock_n8n, mock_local):

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
        with self.captureOnCommitCallbacks(execute=True):
            resp = self.client.post(
                url,
                data=json.dumps({"finding_ids": [finding.id], "filters": {}}),
                content_type="application/json",
            )

        self.assertEqual(resp.status_code, 200)
        mock_n8n.delay.assert_called_once()
        mock_local.delay.assert_not_called()


class DeleteWhileRetriagingTests(LocalTriageFixtures, AISTApiBase):

    """A finished pipeline is not deleted while a batch of its retriage is open, nor one that has not finished."""

    def setUp(self):
        super().setUp()
        self.pipeline = self.make_pipeline("delete-retriage", status=AISTStatus.FINISHED_WITH_WARNINGS)
        self.batch = self._batch(self.pipeline, self._pipeline_findings(self.pipeline, 1))

    def test_the_api_refuses_while_a_batch_is_open_and_deletes_once_it_completed(self):
        url = reverse("aist_api:pipeline_status", kwargs={"pipeline_id": self.pipeline.id})

        self.assertEqual(self.client.delete(url).status_code, 400)
        self.assertTrue(AISTPipeline.objects.filter(pk=self.pipeline.pk).exists())

        LocalTriageBatch.objects.filter(pk=self.batch.pk).update(state=LocalTriageBatchState.COMPLETED)
        self.assertEqual(self.client.delete(url).status_code, 204)

    def test_the_admin_page_refuses_while_a_batch_is_open(self):
        self.login_as_superuser()

        resp = self.client.post(reverse("aist:pipeline_delete", kwargs={"pipeline_id": self.pipeline.id}))

        self.assertEqual(resp.status_code, 400)
        self.assertTrue(AISTPipeline.objects.filter(pk=self.pipeline.pk).exists())

    def test_the_admin_page_refuses_a_pipeline_that_has_not_finished(self):
        self.login_as_superuser()
        running = self.make_pipeline("delete-running", status=AISTStatus.EXECUTING)

        resp = self.client.post(reverse("aist:pipeline_delete", kwargs={"pipeline_id": running.id}))

        self.assertEqual(resp.status_code, 400)
        self.assertTrue(AISTPipeline.objects.filter(pk=running.pk).exists())


class ManualPushTests(AISTApiBase):

    """A maintainer of one organization pushes the findings of a pipeline waiting for confirmation."""

    def setUp(self):
        super().setUp()
        Organization.objects.create(name="Pushing Org", product_type=self.prod_type)
        Organization.objects.create(name="Other Org", product_type=self.other_prod_type)
        self.project.profile = {"ai_triage": {"type": "local"}}
        self.project.save(update_fields=["profile"])
        self.pipeline = self.make_pipeline("manual-push", status=AISTStatus.WAITING_CONFIRMATION_TO_PUSH_TO_AI)
        self.local_push = self.enterContext(patch("aist.tasks.ai.push_request_to_local_triage"))

    def _send(self, finding_ids, pipeline=None):
        with self.captureOnCommitCallbacks(execute=True):
            return self.client.post(
                reverse("aist_api:pipeline_send_request", kwargs={"pipeline_id": (pipeline or self.pipeline).id}),
                data={"finding_ids": finding_ids, "filters": {}},
                format="json",
            )

    def test_findings_of_another_organization_are_never_pushed(self):
        _, own = make_test_with_finding(self.product, self.user)
        _, other_organization_finding = make_test_with_finding(self.other_product, self.user)

        resp = self._send([own.id, other_organization_finding.id])

        self.assertEqual(resp.status_code, 200)
        self.local_push.delay.assert_called_once_with(self.pipeline.id, [own.id])

    def test_a_second_push_while_the_first_is_in_triage_leaves_the_pipeline_running(self):
        _, own = make_test_with_finding(self.product, self.user)
        self._send([own.id])

        resp = self._send([own.id])

        self.assertEqual(resp.status_code, 409)
        self.local_push.delay.assert_called_once()
        self.pipeline.refresh_from_db()
        self.assertEqual(self.pipeline.status, AISTStatus.PUSH_TO_AI)

    def test_a_push_the_broker_refuses_finishes_the_pipeline_with_warnings(self):
        _, own = make_test_with_finding(self.product, self.user)
        self.local_push.delay.side_effect = OperationalError("broker down")

        resp = self._send([own.id])

        self.assertEqual(resp.status_code, 200)
        self.pipeline.refresh_from_db()
        self.assertEqual(self.pipeline.status, AISTStatus.FINISHED_WITH_WARNINGS)

    def test_a_push_to_a_pipeline_before_confirmation_is_refused_and_leaves_it_running(self):
        _, own = make_test_with_finding(self.product, self.user)
        executing = self.make_pipeline("manual-push-early", status=AISTStatus.EXECUTING)

        resp = self._send([own.id], pipeline=executing)

        self.assertEqual(resp.status_code, 400)
        self.local_push.delay.assert_not_called()
        executing.refresh_from_db()
        self.assertEqual(executing.status, AISTStatus.EXECUTING)

    def test_a_push_to_a_finished_pipeline_is_refused_as_finished(self):
        _, own = make_test_with_finding(self.product, self.user)
        finished = self.make_pipeline("manual-push-late", status=AISTStatus.FINISHED)

        resp = self._send([own.id], pipeline=finished)

        self.assertEqual(resp.status_code, 409)
        self.assertIn("finished", resp.json()["error"])
        self.local_push.delay.assert_not_called()
        finished.refresh_from_db()
        self.assertEqual(finished.status, AISTStatus.FINISHED)


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
