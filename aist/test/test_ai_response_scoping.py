"""
Regression: sync_ai_finding_responses must scope its stale-row deletion to
the AISTAIResponse it was passed, not delete every AISTAIFindingResponse on
the pipeline. Otherwise multiple AI sources on one pipeline clobber each
other.
"""
from __future__ import annotations

from django.utils import timezone
from dojo.models import Engagement, Finding, Test, Test_Type

from aist.models import (
    AISTAIFindingResponse,
    AISTAIResponse,
    AISTPipeline,
    AISTStatus,
)
from aist.test.test_api import AISTApiBase
from aist.utils.ai_response import sync_ai_finding_responses


class SyncAIFindingResponsesScopingTests(AISTApiBase):

    """Two AISTAIResponse parents on one pipeline must not clobber each other."""

    def setUp(self):
        super().setUp()
        self.pipeline = AISTPipeline.objects.create(
            id="pipe-scoping",
            project=self.project,
            status=AISTStatus.FINISHED,
        )
        engagement = Engagement.objects.create(
            name="Engage scoping",
            target_start=timezone.now(),
            target_end=timezone.now(),
            product=self.product,
        )
        test_type = Test_Type.objects.create(name="scoping test type")
        self.test = Test.objects.create(
            engagement=engagement,
            target_start=timezone.now(),
            target_end=timezone.now(),
            test_type=test_type,
        )
        self.pipeline.tests.add(self.test)

        self.finding_a_kept = Finding.objects.create(
            test=self.test,
            title="Finding kept under A",
            severity="High",
            date=timezone.now(),
            reporter=self.user,
        )
        self.finding_a_stale = Finding.objects.create(
            test=self.test,
            title="Finding stale under A",
            severity="High",
            date=timezone.now(),
            reporter=self.user,
        )
        self.finding_b_other = Finding.objects.create(
            test=self.test,
            title="Finding owned by B",
            severity="Low",
            date=timezone.now(),
            reporter=self.user,
        )

        self.response_a = AISTAIResponse.objects.create(
            pipeline=self.pipeline,
            payload={"results": {"true_positives": []}},
        )
        self.response_b = AISTAIResponse.objects.create(
            pipeline=self.pipeline,
            payload={"results": {"true_positives": []}},
        )

        # Pre-populate AISTAIFindingResponse rows under each parent.
        AISTAIFindingResponse.objects.create(
            pipeline=self.pipeline,
            source_response=self.response_a,
            finding=self.finding_a_kept,
            verdict=AISTAIFindingResponse.Verdict.TRUE_POSITIVE,
            title="A kept",
        )
        AISTAIFindingResponse.objects.create(
            pipeline=self.pipeline,
            source_response=self.response_a,
            finding=self.finding_a_stale,
            verdict=AISTAIFindingResponse.Verdict.FALSE_POSITIVE,
            title="A stale",
        )
        AISTAIFindingResponse.objects.create(
            pipeline=self.pipeline,
            source_response=self.response_b,
            finding=self.finding_b_other,
            verdict=AISTAIFindingResponse.Verdict.TRUE_POSITIVE,
            title="B other",
        )

    def _resync_a(self, payload):
        self.response_a.payload = payload
        self.response_a.save(update_fields=["payload"])
        return sync_ai_finding_responses(pipeline=self.pipeline, ai_response=self.response_a)

    def test_normal_resync_only_deletes_stale_rows_from_same_source(self):
        # A's new payload references only finding_a_kept; finding_a_stale must
        # be removed but B's row must remain.
        stats = self._resync_a({
            "results": {
                "true_positives": [
                    {
                        "title": "A kept (refreshed)",
                        "reasoning": "ok",
                        "originalFinding": {"id": self.finding_a_kept.id},
                    },
                ],
            },
        })

        self.assertEqual(stats.saved, 1)
        self.assertEqual(stats.deleted, 1)
        self.assertTrue(
            AISTAIFindingResponse.objects.filter(pipeline=self.pipeline, finding=self.finding_a_kept).exists(),
        )
        self.assertFalse(
            AISTAIFindingResponse.objects.filter(pipeline=self.pipeline, finding=self.finding_a_stale).exists(),
        )
        self.assertTrue(
            AISTAIFindingResponse.objects.filter(pipeline=self.pipeline, finding=self.finding_b_other).exists(),
            "B-sourced AISTAIFindingResponse must NOT be deleted by a sync of A.",
        )

    def test_empty_payload_only_clears_same_source(self):
        # No entries at all in A's payload — every A-sourced row should go,
        # but B's row must survive.
        stats = self._resync_a({"results": {}})

        self.assertEqual(stats.saved, 0)
        self.assertEqual(stats.deleted, 2, "Both A-sourced rows should be cleared.")
        self.assertFalse(
            AISTAIFindingResponse.objects.filter(pipeline=self.pipeline, source_response=self.response_a).exists(),
        )
        self.assertTrue(
            AISTAIFindingResponse.objects.filter(pipeline=self.pipeline, finding=self.finding_b_other).exists(),
            "B-sourced AISTAIFindingResponse must survive when A sees no entries.",
        )

    def test_payload_with_only_unresolvable_ids_only_clears_same_source(self):
        # A's entries have no resolvable finding ids — same outcome as empty,
        # but exercises the second early-return branch in sync_ai_finding_responses.
        stats = self._resync_a({
            "results": {
                "true_positives": [
                    {"title": "no id", "originalFinding": {}},
                ],
            },
        })

        self.assertEqual(stats.saved, 0)
        self.assertEqual(stats.dropped, 1)
        self.assertFalse(
            AISTAIFindingResponse.objects.filter(pipeline=self.pipeline, source_response=self.response_a).exists(),
        )
        self.assertTrue(
            AISTAIFindingResponse.objects.filter(pipeline=self.pipeline, finding=self.finding_b_other).exists(),
            "B-sourced AISTAIFindingResponse must survive when A's entries are all unresolvable.",
        )
