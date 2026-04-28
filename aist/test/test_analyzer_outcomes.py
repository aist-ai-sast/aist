from __future__ import annotations

import json
from pathlib import Path
from tempfile import mkdtemp
from types import SimpleNamespace

from django.utils import timezone
from dojo.models import Engagement, Finding, Test, Test_Type

from aist.models import AISTAIFindingResponse, AISTAIResponse, AISTPipeline, AISTStatus
from aist.test.test_api import AISTApiBase
from aist.utils.analyzer_outcomes import consume_analyzer_outcomes
from aist.utils.pipeline import finish_pipeline


class ConsumeAnalyzerOutcomesTests(AISTApiBase):
    def setUp(self):
        super().setUp()
        self.pipeline = AISTPipeline.objects.create(
            id="pipe-outcomes",
            project=self.project,
            status=AISTStatus.FINISHED,
        )
        engagement = Engagement.objects.create(
            name="Analyzer outcomes",
            target_start=timezone.now(),
            target_end=timezone.now(),
            product=self.product,
        )
        test_type = Test_Type.objects.create(name="agent analyzer")
        self.test_obj = Test.objects.create(
            engagement=engagement,
            target_start=timezone.now(),
            target_end=timezone.now(),
            test_type=test_type,
        )
        self.finding = Finding.objects.create(
            test=self.test_obj,
            title="Agent finding",
            severity="High",
            date=timezone.now(),
            reporter=self.user,
            unique_id_from_tool="UID-AGENT",
        )

    def test_degraded_outcome_is_persisted_and_affects_finish_pipeline(self):
        self.pipeline.status = AISTStatus.UPLOADING_RESULTS
        self.pipeline.save(update_fields=["status"])

        consume_analyzer_outcomes(
            pipeline_id=self.pipeline.id,
            outcomes=[
                {
                    "name": "agent-security",
                    "degraded": True,
                    "status": "missing_result",
                    "messages": [{"code": "missing_result", "text": "missing report"}],
                },
            ],
            import_results=[],
            output_dir=self._scratch_dir(),
            user=self.user,
        )

        self.pipeline.refresh_from_db()
        self.assertEqual(
            self.pipeline.launch_data["analyzer_degraded_reasons"][0]["source"],
            "analyzer:agent-security",
        )

        finish_pipeline(self.pipeline.id)
        self.pipeline.refresh_from_db()
        self.assertEqual(self.pipeline.status, AISTStatus.FINISHED_WITH_WARNINGS)

    def test_ai_response_artifact_is_applied_from_outcome_metadata(self):
        output_dir = Path(self._scratch_dir())
        output_dir.mkdir(parents=True, exist_ok=True)
        artifact_name = "agent_ai_response.json"
        (output_dir / artifact_name).write_text(
            json.dumps({
                "results": {
                    "true_positives": [
                        {
                            "uniqueIdFromTool": "UID-AGENT",
                            "title": "Agent finding",
                            "reasoning": "## Verdict\nTP",
                            "references": [],
                            "fix": None,
                        },
                    ],
                    "false_positives": [],
                    "uncertainly": [],
                },
            }),
            encoding="utf-8",
        )

        result = consume_analyzer_outcomes(
            pipeline_id=self.pipeline.id,
            outcomes=[
                {
                    "name": "agent-security",
                    "degraded": False,
                    "status": "success",
                    "artifacts": {
                        "ai_response": {
                            "path": artifact_name,
                            "format": "aist_ai_finding_response_v1",
                            "match_key": "unique_id_from_tool",
                        },
                    },
                },
            ],
            import_results=[
                SimpleNamespace(analyzer_name="agent-security", test_id=self.test_obj.id),
            ],
            output_dir=str(output_dir),
            user=self.user,
        )

        self.assertEqual(result.ai_artifacts_applied, 1)
        ai_response = AISTAIResponse.objects.get(pipeline=self.pipeline)
        self.assertEqual(ai_response.source, AISTAIResponse.Source.AGENT_ANALYZER)
        ai_finding = AISTAIFindingResponse.objects.get(pipeline=self.pipeline)
        self.assertEqual(ai_finding.finding_id, self.finding.id)
        self.assertEqual(ai_finding.verdict, AISTAIFindingResponse.Verdict.TRUE_POSITIVE)

    def _scratch_dir(self) -> str:
        if not hasattr(self, "_tmpdir"):
            self._tmpdir = mkdtemp(prefix="aist-outcomes-test-")
        return self._tmpdir
