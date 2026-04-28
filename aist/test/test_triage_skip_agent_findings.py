"""
Findings pre-triaged by an agent-bridge analyzer (any of them) skip the
regular post-import AI triage queue.

The skip rule is source-based — it keys off
``AISTAIResponse.Source.AGENT_ANALYZER``, not the analyzer name. Adding a
new agent analyzer (e.g. ``claude-full-security`` alongside
``claude-diff-security``) inherits this behavior automatically.
"""
from __future__ import annotations

from unittest.mock import patch

from django.utils import timezone
from dojo.models import Engagement, Finding, Test, Test_Type

from aist.models import (
    AISTAIFindingResponse,
    AISTAIResponse,
    AISTPipeline,
    AISTStatus,
)
from aist.tasks.ai import _prepare_auto_push
from aist.test.test_api import AISTApiBase
from aist.test.test_local_triage import _noop_logger


class TriageSkipAgentAnalyzerFindingsTests(AISTApiBase):

    def setUp(self):
        super().setUp()
        self.engagement = Engagement.objects.create(
            name="E-skip", target_start=timezone.now(), target_end=timezone.now(),
            product=self.product,
        )
        self.test_type = Test_Type.objects.create(name="skip-agent-findings test type")
        self.test_obj = Test.objects.create(
            engagement=self.engagement,
            target_start=timezone.now(),
            target_end=timezone.now(),
            test_type=self.test_type,
        )

    def _make_pipeline(self):
        pipeline = AISTPipeline.objects.create(
            project=self.project,
            project_version=self.pv,
            status=AISTStatus.WAITING_CONFIRMATION_TO_PUSH_TO_AI,
            launch_data={"ai": {"mode": "AUTO_DEFAULT", "triage_type": "local"}},
        )
        # Local triage path is used so we don't need a filter_snapshot.
        self.project.profile = {"ai_triage": {"type": "local"}}
        self.project.save(update_fields=["profile"])
        pipeline.tests.add(self.test_obj)
        return pipeline

    def _make_finding(self, title: str) -> Finding:
        return Finding.objects.create(
            test=self.test_obj, title=title, severity="High",
            date=timezone.now(), reporter=self.user, active=True,
        )

    @patch("aist.tasks.ai.push_request_to_local_triage")
    def test_agent_sourced_findings_are_excluded_from_local_triage_queue(self, mock_local):
        pipeline = self._make_pipeline()
        finding_agent = self._make_finding("agent-sourced finding")
        finding_other = self._make_finding("regular finding")

        agent_response = AISTAIResponse.objects.create(
            pipeline=pipeline,
            payload={"results": {}},
            source=AISTAIResponse.Source.AGENT_ANALYZER,
        )
        AISTAIFindingResponse.objects.create(
            pipeline=pipeline,
            source_response=agent_response,
            finding=finding_agent,
            verdict=AISTAIFindingResponse.Verdict.TRUE_POSITIVE,
            title="pre-triaged",
        )

        with self.captureOnCommitCallbacks(execute=True):
            result = _prepare_auto_push(str(pipeline.id), _noop_logger())
        self.assertIsNone(result, "Local triage should still dispatch (one untagged finding remains).")
        # The local triage delay() must have been called with finding_ids that
        # contain only the non-agent finding.
        mock_local.delay.assert_called_once()
        called_finding_ids = mock_local.delay.call_args.args[1]
        self.assertIn(finding_other.id, called_finding_ids)
        self.assertNotIn(finding_agent.id, called_finding_ids)

    @patch("aist.tasks.ai.push_request_to_local_triage")
    def test_other_source_findings_are_NOT_excluded(self, mock_local):
        # AI responses from the existing post-import triage source (default
        # AI_TRIAGE) MUST NOT cause exclusion — only AGENT_ANALYZER does.
        pipeline = self._make_pipeline()
        finding = self._make_finding("regularly triaged earlier")
        regular_response = AISTAIResponse.objects.create(
            pipeline=pipeline,
            payload={"results": {}},
        )
        # source defaults to AI_TRIAGE.
        self.assertEqual(regular_response.source, AISTAIResponse.Source.AI_TRIAGE)
        AISTAIFindingResponse.objects.create(
            pipeline=pipeline,
            source_response=regular_response,
            finding=finding,
            verdict=AISTAIFindingResponse.Verdict.TRUE_POSITIVE,
            title="from regular triage",
        )

        with self.captureOnCommitCallbacks(execute=True):
            result = _prepare_auto_push(str(pipeline.id), _noop_logger())
        self.assertIsNone(result)
        mock_local.delay.assert_called_once()
        called_finding_ids = mock_local.delay.call_args.args[1]
        self.assertIn(finding.id, called_finding_ids)
