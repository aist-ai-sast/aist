from __future__ import annotations

import json
from pathlib import Path
from tempfile import mkdtemp
from types import SimpleNamespace

from django.utils import timezone
from dojo.models import Engagement, Finding, Test, Test_Type

from aist.models import (
    AISTAIFindingResponse,
    AISTAIResponse,
    AISTPipeline,
    AISTStatus,
    PipelineExecutionType,
)
from aist.services.dast_outcomes import (
    DastPipelineOutcomeCode,
    classify_dast_execution_result,
    public_dast_outcome_code,
    record_dast_pipeline_outcome,
)
from aist.tasks.pipeline import DastConnectorOutcomeState, _handle_dast_execution_result
from aist.test.test_api import AISTApiBase
from aist.utils.analyzer_outcomes import consume_analyzer_outcomes
from aist.utils.pipeline import finish_pipeline


class ConsumeAnalyzerOutcomesTests(AISTApiBase):
    def setUp(self):
        super().setUp()
        self.pipeline = AISTPipeline.objects.create(
            id="pipe-outcomes",
            project=self.project,
            project_version=self.pv,
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


class StandaloneDastOutcomeTests(AISTApiBase):
    def setUp(self):
        super().setUp()
        self.pipeline = AISTPipeline.objects.create(
            id="dast-outcome",
            project=self.project,
            trigger_project_version=self.pv,
            execution_type=PipelineExecutionType.DAST,
            status=AISTStatus.EXECUTING,
        )
        self.other_pipeline = AISTPipeline.objects.create(
            id="dast-outcome-other",
            project=self.project,
            trigger_project_version=self.pv,
            execution_type=PipelineExecutionType.DAST,
            status=AISTStatus.EXECUTING,
        )

    @staticmethod
    def _result(*, status: str, reason_code: str | None = None, findings=None):
        return SimpleNamespace(
            outcome=SimpleNamespace(
                state=SimpleNamespace(value="terminal"),
                reason_code=reason_code,
            ),
            terminal_result=SimpleNamespace(
                status=SimpleNamespace(value=status),
                report={"findings": findings or []},
                selection={"relation": "exact", "distance": 0},
            ),
            recovery=SimpleNamespace(log_cursor=2),
            telemetry=SimpleNamespace(logs_delivered=2, max_log_lag_seconds=0.5),
        )

    @staticmethod
    def _runtime_result(*, status: str, reason_code: str | None = None, findings=None):
        result = StandaloneDastOutcomeTests._result(
            status=status,
            reason_code=reason_code,
            findings=findings,
        )
        result.outcome.state = DastConnectorOutcomeState.TERMINAL
        return result

    def test_terminal_scenarios_have_stable_codes_and_degraded_semantics(self):
        scenarios = [
            (self._result(status="succeeded", findings=[{"title": "finding"}]), "SUCCESS_WITH_FINDINGS", False),
            (self._result(status="succeeded"), "SUCCESS_CLEAN", False),
            (self._result(status="failed", reason_code="NO_ELIGIBLE_STAND"), "POLICY_NO_ELIGIBLE_STAND", True),
            (self._result(status="failed", reason_code="SOURCE_DRIFT"), "SOURCE_DRIFT", True),
            (self._result(status="failed", reason_code="REPORT_INVALID"), "INVALID_RESULT", True),
            (
                self._result(status="completed_with_degradation", findings=[{"title": "finding"}]),
                "COMPLETED_WITH_DEGRADATION",
                True,
            ),
            (
                self._result(status="failed_with_partial_results", findings=[{"title": "finding"}]),
                "FAILED_WITH_PARTIAL_RESULTS",
                True,
            ),
            (
                self._result(status="failed", reason_code="PROVIDER_CREDENTIALS_EXPIRED"),
                "PROVIDER_CREDENTIALS_EXPIRED",
                True,
            ),
            (self._result(status="failed", reason_code="provider-secret-detail"), "PROVIDER_FAILED", True),
            (self._result(status="unknown"), "INVALID_RESULT", True),
            (self._result(status="stopped", reason_code="CANCEL_REQUESTED"), "CANCELLED", True),
            (self._result(status="stopped", reason_code="CANCEL_REQUESTED"), "CANCELLED", True),
        ]

        for result, expected_code, expected_degraded in scenarios:
            with self.subTest(expected_code=expected_code):
                outcome = classify_dast_execution_result(result)
                self.assertEqual(outcome.code, expected_code)
                self.assertEqual(outcome.degraded, expected_degraded)

    def test_outcome_persistence_is_pipeline_local_and_never_keeps_raw_provider_reason(self):
        outcome = classify_dast_execution_result(
            self._result(status="failed", reason_code="internal-provider-stacktrace"),
        )
        record_dast_pipeline_outcome(self.pipeline.id, outcome.code)

        self.pipeline.refresh_from_db()
        self.other_pipeline.refresh_from_db()
        self.assertEqual(public_dast_outcome_code(self.pipeline), DastPipelineOutcomeCode.PROVIDER_FAILED)
        self.assertNotIn("internal-provider-stacktrace", str(self.pipeline.launch_data))
        self.assertIsNone(public_dast_outcome_code(self.other_pipeline))

    def test_provider_failure_finishes_only_the_dast_pipeline_with_warnings(self):
        _handle_dast_execution_result(
            SimpleNamespace(),
            self.pipeline.id,
            self._runtime_result(status="failed", reason_code="NO_ELIGIBLE_STAND"),
        )

        self.pipeline.refresh_from_db()
        self.other_pipeline.refresh_from_db()
        self.assertEqual(self.pipeline.status, AISTStatus.FINISHED_WITH_WARNINGS)
        self.assertEqual(public_dast_outcome_code(self.pipeline), "POLICY_NO_ELIGIBLE_STAND")
        self.assertEqual(self.other_pipeline.status, AISTStatus.EXECUTING)
