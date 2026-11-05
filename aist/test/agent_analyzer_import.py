"""
One agent-bridge analyzer's report-first flow, imported and synced through the generic helpers.

A subclass supplies the analyzer and its two output files' content; the flow and its assertions are shared, so every
agent analyzer goes through the same ``upload_results_internal`` + ``apply_ai_response_artifact`` path without an
analyzer-name branch.
"""

from __future__ import annotations

import json
import logging
import shutil
import tempfile
from pathlib import Path

from dojo.models import Finding

from aist.internal_upload import NO_EXCLUSION, upload_results_internal
from aist.models import AISTAIFindingResponse, AISTAIResponse, AISTPipeline, AISTStatus
from aist.test.analyzer_catalog import write_analyzers_config
from aist.test.scan_import_helpers import write_generic_report
from aist.utils.ai_response_artifact import apply_ai_response_artifact


def write_analyzers_yaml(directory: Path, *, name: str, skill_name: str, result_file: str, output_type: str) -> str:
    """Write a one-entry analyzers.yaml that ``upload_results_internal`` iterates, and return its path."""
    return write_analyzers_config(directory, [{
        "name": name,
        "type": "agent-bridge",
        "enabled": True,
        "time_class": "slow",
        "skill_name": skill_name,
        "output_type": output_type,
        "result_file": result_file,
    }])


def write_ai_response(path: Path, results: dict) -> None:
    """Write an analyzer's AI triage ``results`` as the AI response file at ``path``."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"results": results}), encoding="utf-8")


class AgentAnalyzerImportAndSync:

    """
    Mixed into an ``AISTApiBase`` test case that sets the class attributes below.

    ``GFI_FINDINGS`` and ``AI_TRUE_POSITIVES`` describe two findings, one confident (``UID_CONFIDENT``, uncertainty
    at most 0.2) and one likely (``UID_UNCERTAIN``, uncertainty in [0.4, 0.7]).
    """

    ANALYZER_NAME: str
    SKILL_NAME: str
    PIPELINE_ID: str
    UID_CONFIDENT: str
    UID_UNCERTAIN: str
    GFI_FINDINGS: list[dict]
    SCAN_TYPE: str
    AI_TRUE_POSITIVES: list[dict]

    def setUp(self):
        super().setUp()
        self.pipeline = AISTPipeline.objects.create(
            id=self.PIPELINE_ID,
            project=self.project,
            project_version=self.pv,
            status=AISTStatus.UPLOADING_RESULTS,
        )
        self.output_dir = Path(tempfile.mkdtemp(prefix=f"{self.ANALYZER_NAME}-e2e-"))
        self.addCleanup(shutil.rmtree, self.output_dir)
        self.repo_dir = self.output_dir / "repo"
        self.repo_dir.mkdir()
        self.result_file = f"{self.ANALYZER_NAME}_result.json"
        self.ai_response_file = f"{self.ANALYZER_NAME}_ai_response.json"
        self.config_path = write_analyzers_yaml(
            self.output_dir,
            name=self.ANALYZER_NAME,
            skill_name=self.SKILL_NAME,
            result_file=self.result_file,
            output_type=self.SCAN_TYPE,
        )
        write_generic_report(self.output_dir / self.result_file, self.GFI_FINDINGS)
        write_ai_response(
            self.output_dir / self.ai_response_file,
            {"true_positives": self.AI_TRUE_POSITIVES, "false_positives": [], "uncertainly": []},
        )

    def test_import_and_sync(self):
        results = upload_results_internal(
            output_dir=str(self.output_dir),
            analyzers_cfg_path=self.config_path,
            product=self.product,
            repo_path=str(self.repo_dir),
            exclusion=NO_EXCLUSION,
            logger=logging.getLogger(__name__),
        )

        self.assertEqual(len(results), 1)
        result = results[0]
        self.assertEqual(result.analyzer_name, self.ANALYZER_NAME)
        self.assertIsNotNone(result.test_id)
        self.assertEqual(result.imported_findings, 2)

        imported_findings = list(Finding.objects.filter(test_id=result.test_id))
        self.assertEqual(
            {f.unique_id_from_tool for f in imported_findings}, {self.UID_CONFIDENT, self.UID_UNCERTAIN},
        )

        sync_result = apply_ai_response_artifact(
            pipeline=self.pipeline,
            output_dir=str(self.output_dir),
            artifact_path=self.ai_response_file,
            test_id=result.test_id,
            user=self.user,
        )
        self.assertIsNotNone(sync_result)
        self.assertEqual(sync_result.saved, 2)

        responses = list(AISTAIResponse.objects.filter(pipeline=self.pipeline))
        self.assertEqual(len(responses), 1)
        self.assertEqual(responses[0].source, AISTAIResponse.Source.AGENT_ANALYZER)

        rows = list(AISTAIFindingResponse.objects.filter(pipeline=self.pipeline))
        self.assertEqual(len(rows), 2)
        verdicts = {row.finding.unique_id_from_tool: row.verdict for row in rows}
        self.assertEqual(verdicts[self.UID_CONFIDENT], AISTAIFindingResponse.Verdict.TRUE_POSITIVE)
        self.assertEqual(verdicts[self.UID_UNCERTAIN], AISTAIFindingResponse.Verdict.TRUE_POSITIVE)

        uncertainty = {row.finding.unique_id_from_tool: row.uncertainty_level for row in rows}
        self.assertLessEqual(uncertainty[self.UID_CONFIDENT], 0.2)
        self.assertGreaterEqual(uncertainty[self.UID_UNCERTAIN], 0.4)
        self.assertLessEqual(uncertainty[self.UID_UNCERTAIN], 0.7)

        for finding in imported_findings:
            finding.refresh_from_db()
            self.assertFalse(finding.false_p, f"Finding {finding.id} unexpectedly marked false_p")
            self.assertFalse(finding.is_mitigated, f"Finding {finding.id} unexpectedly mitigated")

        for uid in (self.UID_CONFIDENT, self.UID_UNCERTAIN):
            row = next(r for r in rows if r.finding.unique_id_from_tool == uid)
            self.assertIsNotNone(row.fix, f"fix missing for {uid}")
            self.assertEqual(row.fix["fixType"], "code_change")
