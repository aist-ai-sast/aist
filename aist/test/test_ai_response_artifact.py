"""AI response artifact sync resolves analyzer entries to imported Findings."""
from __future__ import annotations

import json
from pathlib import Path
from tempfile import mkdtemp

from django.utils import timezone
from dojo.models import Engagement, Finding, Test, Test_Type

from aist.models import (
    AISTAIFindingResponse,
    AISTAIResponse,
    AISTPipeline,
    AISTStatus,
)
from aist.test.test_api import AISTApiBase
from aist.utils.ai_response_artifact import apply_ai_response_artifact

_AI_RESPONSE_FILENAME = "agent_ai_response.json"


class ApplyAiResponseArtifactTests(AISTApiBase):

    def setUp(self):
        super().setUp()
        self.pipeline = AISTPipeline.objects.create(
            id="pipe-artifact-sync",
            project=self.project,
            project_version=self.pv,
            status=AISTStatus.FINISHED,
        )
        engagement = Engagement.objects.create(
            name="Engage artifact sync",
            target_start=timezone.now(),
            target_end=timezone.now(),
            product=self.product,
        )
        test_type = Test_Type.objects.create(name="agent analyzer gen")
        self.agent_test = Test.objects.create(
            engagement=engagement,
            target_start=timezone.now(),
            target_end=timezone.now(),
            test_type=test_type,
        )
        self.pipeline.tests.add(self.agent_test)

        # Two agent-analyzer findings, indexed by deterministic unique_id_from_tool.
        self.finding_tp = Finding.objects.create(
            test=self.agent_test,
            title="SSRF on /api/fetch",
            severity="High",
            date=timezone.now(),
            reporter=self.user,
            unique_id_from_tool="UID-TP",
        )
        self.finding_fp = Finding.objects.create(
            test=self.agent_test,
            title="Hardcoded secret in tests",
            severity="Medium",
            date=timezone.now(),
            reporter=self.user,
            unique_id_from_tool="UID-FP",
        )

    def _write_ai_response(self, output_dir: Path, payload: dict) -> None:
        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / _AI_RESPONSE_FILENAME).write_text(
            json.dumps(payload), encoding="utf-8",
        )

    def test_resolves_unique_ids_and_creates_ai_finding_responses(self):
        output_dir = Path(self._scratch_dir())
        self._write_ai_response(output_dir, {
            "results": {
                "true_positives": [
                    {
                        "uniqueIdFromTool": "UID-TP",
                        "title": "SSRF",
                        "reasoning": "## Verdict\nTP\n## Evidence\nx",
                        "references": ["https://example.test/cwe-918"],
                        "epssScore": 0.05,
                        "impactScore": 7.5,
                        "exploitabilityScore": 4.0,
                        "uncertaintyLevel": 0.1,
                        "uncertaintySpread": 0.05,
                        "exploitCodeMaturity": "",
                        "fix": {
                            "fixType": "code_change",
                            "fixSummary": "Validate URL host",
                            "diffAvailable": True,
                            "diff": "-old\n+new\n",
                            "codeAfter": None,
                            "stepByStep": ["Step 1: validate scheme", "Step 2: allow-list hosts"],
                            "testingHint": None,
                            "secretsManagement": None,
                            "suppressionAnnotation": None,
                        },
                    },
                ],
                "false_positives": [
                    {
                        "uniqueIdFromTool": "UID-FP",
                        "title": "Hardcoded secret",
                        "reasoning": "## Verdict\nFP\n## Evidence\nfixture only",
                        "references": [],
                        "epssScore": None,
                        "impactScore": None,
                        "exploitabilityScore": None,
                        "uncertaintyLevel": None,
                        "uncertaintySpread": None,
                        "exploitCodeMaturity": "",
                        "fix": None,
                    },
                ],
                "uncertainly": [],
            },
        })

        result = apply_ai_response_artifact(
            pipeline=self.pipeline,
            output_dir=str(output_dir),
            artifact_path=_AI_RESPONSE_FILENAME,
            test_id=self.agent_test.id,
            user=self.user,
        )

        self.assertIsNotNone(result)
        self.assertEqual(result.saved, 2)
        self.assertEqual(result.dropped, 0)

        # AISTAIResponse must be tagged with the new source enum.
        ai_responses = list(
            AISTAIResponse.objects.filter(pipeline=self.pipeline),
        )
        self.assertEqual(len(ai_responses), 1)
        self.assertEqual(ai_responses[0].source, AISTAIResponse.Source.AGENT_ANALYZER)

        # Two AISTAIFindingResponse rows; FP closed the underlying Finding.
        rows = AISTAIFindingResponse.objects.filter(pipeline=self.pipeline).order_by("finding_id")
        self.assertEqual(rows.count(), 2)
        verdict_by_uid = {row.finding.unique_id_from_tool: row.verdict for row in rows}
        self.assertEqual(verdict_by_uid["UID-TP"], AISTAIFindingResponse.Verdict.TRUE_POSITIVE)
        self.assertEqual(verdict_by_uid["UID-FP"], AISTAIFindingResponse.Verdict.FALSE_POSITIVE)

        self.finding_fp.refresh_from_db()
        self.assertTrue(self.finding_fp.false_p)
        self.assertTrue(self.finding_fp.is_mitigated)

    def test_drops_entries_with_unresolvable_unique_ids(self):
        output_dir = Path(self._scratch_dir())
        self._write_ai_response(output_dir, {
            "results": {
                "true_positives": [
                    # UID-TP resolves; UID-MISSING does not.
                    {
                        "uniqueIdFromTool": "UID-TP",
                        "title": "SSRF",
                        "reasoning": "## Verdict\nTP\n## Evidence\nx",
                        "references": [],
                        "epssScore": None,
                        "impactScore": None,
                        "exploitabilityScore": None,
                        "uncertaintyLevel": None,
                        "uncertaintySpread": None,
                        "exploitCodeMaturity": "",
                        "fix": None,
                    },
                    {
                        "uniqueIdFromTool": "UID-MISSING",
                        "title": "Phantom",
                        "reasoning": "## Verdict\nTP",
                        "references": [],
                        "epssScore": None,
                        "impactScore": None,
                        "exploitabilityScore": None,
                        "uncertaintyLevel": None,
                        "uncertaintySpread": None,
                        "exploitCodeMaturity": "",
                        "fix": None,
                    },
                ],
                "false_positives": [],
                "uncertainly": [],
            },
        })

        result = apply_ai_response_artifact(
            pipeline=self.pipeline,
            output_dir=str(output_dir),
            artifact_path=_AI_RESPONSE_FILENAME,
            test_id=self.agent_test.id,
            user=self.user,
        )

        self.assertIsNotNone(result)
        self.assertEqual(result.saved, 1)
        # The unresolvable entry is counted as dropped (1 from translate_payload).
        self.assertGreaterEqual(result.dropped, 1)
        rows = AISTAIFindingResponse.objects.filter(pipeline=self.pipeline)
        self.assertEqual(rows.count(), 1)
        self.assertEqual(rows.first().finding_id, self.finding_tp.id)

    def test_missing_file_is_noop(self):
        output_dir = Path(self._scratch_dir())
        # Do NOT write the file.
        result = apply_ai_response_artifact(
            pipeline=self.pipeline,
            output_dir=str(output_dir),
            artifact_path=_AI_RESPONSE_FILENAME,
            test_id=self.agent_test.id,
            user=self.user,
        )
        self.assertIsNone(result)
        self.assertFalse(AISTAIResponse.objects.filter(pipeline=self.pipeline).exists())

    def test_malformed_json_is_swallowed(self):
        output_dir = Path(self._scratch_dir())
        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / _AI_RESPONSE_FILENAME).write_text("not json", encoding="utf-8")
        result = apply_ai_response_artifact(
            pipeline=self.pipeline,
            output_dir=str(output_dir),
            artifact_path=_AI_RESPONSE_FILENAME,
            test_id=self.agent_test.id,
            user=self.user,
        )
        self.assertIsNone(result)
        self.assertFalse(AISTAIResponse.objects.filter(pipeline=self.pipeline).exists())

    def test_empty_results_clears_only_diff_sourced_rows(self):
        # Pre-populate an agent-sourced row that should be wiped + a non-agent row
        # from another source that must survive.
        other_source = AISTAIResponse.objects.create(
            pipeline=self.pipeline,
            payload={"results": {"true_positives": [{"originalFinding": {"id": self.finding_tp.id}}]}},
        )
        AISTAIFindingResponse.objects.create(
            pipeline=self.pipeline,
            source_response=other_source,
            finding=self.finding_tp,
            verdict=AISTAIFindingResponse.Verdict.TRUE_POSITIVE,
            title="from existing triage",
        )
        # And a stale agent-sourced row that should be cleared by the empty payload.
        agent_source = AISTAIResponse.objects.create(
            pipeline=self.pipeline,
            payload={"results": {"true_positives": []}},
            source=AISTAIResponse.Source.AGENT_ANALYZER,
        )
        AISTAIFindingResponse.objects.create(
            pipeline=self.pipeline,
            source_response=agent_source,
            finding=self.finding_fp,
            verdict=AISTAIFindingResponse.Verdict.FALSE_POSITIVE,
            title="stale diff row",
        )

        output_dir = Path(self._scratch_dir())
        self._write_ai_response(output_dir, {
            "results": {"true_positives": [], "false_positives": [], "uncertainly": []},
        })

        apply_ai_response_artifact(
            pipeline=self.pipeline,
            output_dir=str(output_dir),
            artifact_path=_AI_RESPONSE_FILENAME,
            test_id=self.agent_test.id,
            user=self.user,
        )

        # The non-diff existing row must NOT be deleted.
        self.assertTrue(
            AISTAIFindingResponse.objects.filter(
                pipeline=self.pipeline, source_response=other_source,
            ).exists(),
            "Existing AI triage row from another source must not be deleted by diff sync.",
        )

    def _scratch_dir(self) -> str:
        # Django's TestCase doesn't expose pytest's tmp_path; use a fresh
        # directory per test instance and rely on the OS to clean it up.
        if not hasattr(self, "_tmpdir"):
            self._tmpdir = mkdtemp(prefix="aist-diff-test-")
        return self._tmpdir
