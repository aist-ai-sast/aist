"""
End-to-end test for the claude-full-security analyzer's report-first flow.

Mirrors ``test_diff_security_e2e.py`` to lock that the full analyzer is
imported through the same generic helpers (``upload_results_internal`` +
``apply_ai_response_artifact``) — without analyzer-name branches. If any
import path grows a "diff-only" code branch, this test catches it.

The bridge invocation itself is covered separately by sast-pipeline tests.
Here we simulate the bridge having ALREADY produced its two output files
(Generic Findings Import + AI response) and verify that:

1. ``upload_results_internal`` ingests the GFI file via DefaultImporter,
   producing a single Test with two vendor Findings (one for each entry,
   keyed by ``unique_id_from_tool``).
2. ``apply_ai_response_artifact`` resolves both ``uniqueIdFromTool`` values
   to those Findings, creates one ``AISTAIResponse(source=AGENT_ANALYZER)``
   and two ``AISTAIFindingResponse`` rows.
"""
from __future__ import annotations

import json
from pathlib import Path
from tempfile import mkdtemp

import yaml
from dojo.models import Finding

from aist.internal_upload import upload_results_internal
from aist.models import (
    AISTAIFindingResponse,
    AISTAIResponse,
    AISTPipeline,
    AISTStatus,
)
from aist.test.test_api import AISTApiBase
from aist.utils.ai_response_artifact import apply_ai_response_artifact

_ANALYZER_NAME = "claude-full-security"
_RESULT_FILENAME = "claude-full-security_result.json"
_AI_RESPONSE_FILENAME = "claude-full-security_ai_response.json"


def _write_minimal_analyzers_yaml(tmp_dir: Path) -> str:
    """A one-entry analyzers.yaml that upload_results_internal can iterate."""
    config = {
        "analyzers": [
            {
                "name": _ANALYZER_NAME,
                "type": "agent-bridge",
                "enabled": True,
                "time_class": "slow",
                "skill_name": "aist-full-security-review",
                "output_type": "Generic Findings Import",
                "result_file": _RESULT_FILENAME,
            },
        ],
    }
    config_path = tmp_dir / "analyzers.yaml"
    config_path.write_text(yaml.safe_dump(config), encoding="utf-8")
    return str(config_path)


class ClaudeFullSecurityE2ETests(AISTApiBase):

    def setUp(self):
        super().setUp()
        self.pipeline = AISTPipeline.objects.create(
            id="pipe-full-e2e",
            project=self.project,
            status=AISTStatus.UPLOADING_RESULTS,
        )

        self.tmp_dir = Path(mkdtemp(prefix="aist-full-e2e-"))
        self.output_dir = self.tmp_dir
        self.repo_dir = self.tmp_dir / "repo"
        self.repo_dir.mkdir(exist_ok=True, parents=True)
        self.config_path = _write_minimal_analyzers_yaml(self.tmp_dir)

        # Two findings produced by a full-project scan — both true_positive
        # per the skill's TP-only emission policy: one with low
        # uncertaintyLevel (confident) and one with high (likely).
        # `unique_id_from_tool` MUST match between the GFI file and the AI
        # response file so the post-import sync can resolve them.
        self.uid_confident = "uid-full-confident-pathtraversal"
        self.uid_uncertain = "uid-full-uncertain-tenant-isolation"
        gfi_payload = {
            "findings": [
                {
                    "title": "Path traversal in /api/files",
                    "severity": "High",
                    "description": "Evidence: ...\nReproduction: ...\nImpact: ...",
                    "file_path": "src/files.py",
                    "line": 88,
                    "cwe": 22,
                    "static_finding": True,
                    "active": True,
                    "verified": False,
                    "unique_id_from_tool": self.uid_confident,
                },
                {
                    "title": "Tenant isolation gap on bulk export endpoint",
                    "severity": "High",
                    "description": "Evidence: ownership check tied to object existence, not caller's tenant.\nReproduction: needs upstream proof that endpoint is reachable from another tenant.\nImpact: cross-tenant data exposure.",
                    "file_path": "services/api/export.py",
                    "line": 33,
                    "static_finding": True,
                    "active": True,
                    "verified": False,
                    "unique_id_from_tool": self.uid_uncertain,
                },
            ],
        }
        (self.output_dir / _RESULT_FILENAME).write_text(
            json.dumps(gfi_payload), encoding="utf-8",
        )

        ai_response_payload = {
            "results": {
                "true_positives": [
                    {
                        "uniqueIdFromTool": self.uid_confident,
                        "title": "Path traversal",
                        "reasoning": "## Verdict\nTP\n## Evidence\nx",
                        "references": ["https://example.test/cwe-22"],
                        "epssScore": 0.07,
                        "impactScore": 7.0,
                        "exploitabilityScore": 5.0,
                        "uncertaintyLevel": 0.1,  # confident tier
                        "uncertaintySpread": 0.05,
                        "exploitCodeMaturity": "",
                        "fix": {
                            "fixType": "code_change",
                            "fixSummary": "Resolve canonical path and verify containment under allow-list root",
                            "diffAvailable": True,
                            "diff": "-old\n+new\n",
                            "codeAfter": None,
                            "stepByStep": [
                                "Step 1: resolve to canonical path",
                                "Step 2: assert canonical startswith allow-list root",
                            ],
                            "testingHint": None,
                            "secretsManagement": None,
                            "suppressionAnnotation": None,
                        },
                    },
                    {
                        "uniqueIdFromTool": self.uid_uncertain,
                        "title": "Tenant isolation gap on bulk export endpoint",
                        "reasoning": "## Verdict\nTP\n## Evidence\nOwnership check is on object, not caller.",
                        "references": [],
                        "epssScore": None,
                        "impactScore": 8.0,
                        "exploitabilityScore": 5.0,
                        "uncertaintyLevel": 0.55,  # likely tier (∈ [0.4, 0.7])
                        "uncertaintySpread": 0.1,
                        "exploitCodeMaturity": "",
                        "fix": {
                            "fixType": "code_change",
                            "fixSummary": "Tie ownership check to caller.tenant, not object.tenant",
                            "diffAvailable": True,
                            "diff": "-object.tenant\n+caller.tenant\n",
                            "codeAfter": None,
                            "stepByStep": [
                                "Step 1: take caller's tenant from session",
                                "Step 2: filter rows by caller.tenant before export",
                            ],
                            "testingHint": None,
                            "secretsManagement": None,
                            "suppressionAnnotation": None,
                        },
                    },
                ],
                "false_positives": [],
                "uncertainly": [],
            },
        }
        (self.output_dir / _AI_RESPONSE_FILENAME).write_text(
            json.dumps(ai_response_payload), encoding="utf-8",
        )

    def test_full_security_import_and_sync(self):
        results = upload_results_internal(
            output_dir=str(self.output_dir),
            analyzers_cfg_path=self.config_path,
            product_name=self.product.name,
            repo_path=str(self.repo_dir),
            trim_path="",
            pipeline_id=self.pipeline.id,
            log_level="INFO",
        )

        # Generic helper recognises the analyzer purely by YAML — no
        # claude-full-security branch in upload_results_internal.
        self.assertEqual(len(results), 1)
        full_result = results[0]
        self.assertEqual(full_result.analyzer_name, _ANALYZER_NAME)
        self.assertIsNotNone(full_result.test_id)
        self.assertEqual(full_result.imported_findings, 2)

        imported_findings = list(Finding.objects.filter(test_id=full_result.test_id))
        self.assertEqual(len(imported_findings), 2)
        uid_to_finding = {f.unique_id_from_tool: f for f in imported_findings}
        self.assertIn(self.uid_confident, uid_to_finding)
        self.assertIn(self.uid_uncertain, uid_to_finding)

        sync_result = apply_ai_response_artifact(
            pipeline=self.pipeline,
            output_dir=str(self.output_dir),
            artifact_path=_AI_RESPONSE_FILENAME,
            test_id=full_result.test_id,
            user=self.user,
        )
        self.assertIsNotNone(sync_result)
        self.assertEqual(sync_result.saved, 2)

        # One AISTAIResponse with the AGENT_ANALYZER source so the post-import
        # triage queue skips these findings (locked separately by Task 7).
        responses = list(AISTAIResponse.objects.filter(pipeline=self.pipeline))
        self.assertEqual(len(responses), 1)
        self.assertEqual(responses[0].source, AISTAIResponse.Source.AGENT_ANALYZER)

        # Two AISTAIFindingResponse rows; both TRUE_POSITIVE per the
        # TP-only emission policy. Confidence differs via uncertaintyLevel.
        rows = list(AISTAIFindingResponse.objects.filter(pipeline=self.pipeline))
        self.assertEqual(len(rows), 2)
        verdicts = {row.finding.unique_id_from_tool: row.verdict for row in rows}
        self.assertEqual(verdicts[self.uid_confident], AISTAIFindingResponse.Verdict.TRUE_POSITIVE)
        self.assertEqual(verdicts[self.uid_uncertain], AISTAIFindingResponse.Verdict.TRUE_POSITIVE)

        # Confidence tier round-trips into uncertaintyLevel.
        uncertainty_by_uid = {row.finding.unique_id_from_tool: row.uncertainty_level for row in rows}
        self.assertLessEqual(uncertainty_by_uid[self.uid_confident], 0.2)
        self.assertGreaterEqual(uncertainty_by_uid[self.uid_uncertain], 0.4)
        self.assertLessEqual(uncertainty_by_uid[self.uid_uncertain], 0.7)

        # Both Findings remain active — TP-only emission means no auto-close.
        for finding in imported_findings:
            finding.refresh_from_db()
            self.assertFalse(finding.false_p, f"Finding {finding.id} unexpectedly marked false_p")
            self.assertFalse(finding.is_mitigated, f"Finding {finding.id} unexpectedly mitigated")

        # TP fix blocks round-trip for both confidence tiers (per schema,
        # fix is required for true_positive).
        for uid in (self.uid_confident, self.uid_uncertain):
            row = next(r for r in rows if r.finding.unique_id_from_tool == uid)
            self.assertIsNotNone(row.fix, f"fix missing for {uid}")
            self.assertEqual(row.fix["fixType"], "code_change")
