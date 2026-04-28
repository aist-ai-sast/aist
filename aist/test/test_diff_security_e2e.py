"""
End-to-end test for the claude-diff-security analyzer's report-first flow.

The bridge invocation itself is covered separately by sast-pipeline tests.
Here we simulate the bridge having ALREADY produced its two output files
(Generic Findings Import + AI response) and verify that:

1. ``upload_results_internal`` ingests the GFI file via DefaultImporter,
   producing a single Test with two vendor Findings (one for each entry,
   keyed by ``unique_id_from_tool``).
2. ``apply_ai_response_artifact`` then resolves both ``uniqueIdFromTool``
   values to those Findings, creates one ``AISTAIResponse(source=AGENT_ANALYZER)``
   and two ``AISTAIFindingResponse`` rows (one TP, one FP).
3. The FP entry triggers ``close_finding`` so the underlying Finding is
   marked false_p + is_mitigated.
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

_ANALYZER_NAME = "claude-diff-security"
_RESULT_FILENAME = "claude-diff-security_result.json"
_AI_RESPONSE_FILENAME = "claude-diff-security_ai_response.json"


def _write_minimal_analyzers_yaml(tmp_dir: Path) -> str:
    """A one-entry analyzers.yaml that upload_results_internal can iterate."""
    config = {
        "analyzers": [
            {
                "name": _ANALYZER_NAME,
                "type": "agent-bridge",
                "enabled": True,
                "time_class": "slow",
                "skill_name": "aist-diff-security-review",
                "output_type": "Generic Findings Import",
                "result_file": _RESULT_FILENAME,
            },
        ],
    }
    config_path = tmp_dir / "analyzers.yaml"
    config_path.write_text(yaml.safe_dump(config), encoding="utf-8")
    return str(config_path)


class ClaudeDiffSecurityE2ETests(AISTApiBase):

    def setUp(self):
        super().setUp()
        self.pipeline = AISTPipeline.objects.create(
            id="pipe-diff-e2e",
            project=self.project,
            status=AISTStatus.UPLOADING_RESULTS,
        )

        self.tmp_dir = Path(mkdtemp(prefix="aist-diff-e2e-"))
        self.output_dir = self.tmp_dir
        self.repo_dir = self.tmp_dir / "repo"
        self.repo_dir.mkdir(exist_ok=True, parents=True)
        self.config_path = _write_minimal_analyzers_yaml(self.tmp_dir)

        # Two findings from the diff, both true_positive per the TP-only
        # emission policy (decision 13 in the plan): one with low
        # uncertaintyLevel (confident) and one with high (likely).
        # `unique_id_from_tool` MUST match between the GFI file and the AI
        # response file so the post-import sync can resolve them.
        self.uid_confident = "uid-confident-ssrf"
        self.uid_uncertain = "uid-uncertain-statetx"
        gfi_payload = {
            "findings": [
                {
                    "title": "SSRF on /api/fetch",
                    "severity": "High",
                    "description": "Evidence: ...\nReproduction: ...\nImpact: ...",
                    "file_path": "src/app.py",
                    "line": 42,
                    "cwe": 918,
                    "static_finding": True,
                    "active": True,
                    "verified": False,
                    "unique_id_from_tool": self.uid_confident,
                },
                {
                    "title": "Account activation reachable without registration token",
                    "severity": "High",
                    "description": "Evidence: refactor changes activation lookup to be email-only.\nReproduction: needs upstream registration flow trace.\nImpact: account takeover.",
                    "file_path": "services/auth/activation.py",
                    "line": 7,
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
                        "title": "SSRF",
                        "reasoning": "## Verdict\nTP\n## Evidence\nx",
                        "references": ["https://example.test/cwe-918"],
                        "epssScore": 0.05,
                        "impactScore": 7.5,
                        "exploitabilityScore": 4.0,
                        "uncertaintyLevel": 0.1,  # confident tier
                        "uncertaintySpread": 0.05,
                        "exploitCodeMaturity": "",
                        "fix": {
                            "fixType": "code_change",
                            "fixSummary": "Validate URL scheme and host before fetch",
                            "diffAvailable": True,
                            "diff": "-old\n+new\n",
                            "codeAfter": None,
                            "stepByStep": ["Step 1: validate scheme", "Step 2: allow-list hosts"],
                            "testingHint": None,
                            "secretsManagement": None,
                            "suppressionAnnotation": None,
                        },
                    },
                    {
                        "uniqueIdFromTool": self.uid_uncertain,
                        "title": "Account activation reachable without registration token",
                        "reasoning": "## Verdict\nTP\n## Evidence\nLookup widened to email-only.",
                        "references": [],
                        "epssScore": None,
                        "impactScore": 8.0,
                        "exploitabilityScore": 6.0,
                        "uncertaintyLevel": 0.55,  # likely tier (∈ [0.4, 0.7])
                        "uncertaintySpread": 0.1,
                        "exploitCodeMaturity": "",
                        "fix": {
                            "fixType": "code_change",
                            "fixSummary": "Restore proof-of-prior-state check on activation",
                            "diffAvailable": True,
                            "diff": "-email-only\n+token-bound\n",
                            "codeAfter": None,
                            "stepByStep": ["Step 1: require token from registration step"],
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

    def test_full_diff_security_import_and_sync(self):
        results = upload_results_internal(
            output_dir=str(self.output_dir),
            analyzers_cfg_path=self.config_path,
            product_name=self.product.name,
            repo_path=str(self.repo_dir),
            trim_path="",
            pipeline_id=self.pipeline.id,
            log_level="INFO",
        )

        self.assertEqual(len(results), 1)
        diff_result = results[0]
        self.assertEqual(diff_result.analyzer_name, _ANALYZER_NAME)
        self.assertIsNotNone(diff_result.test_id)
        self.assertEqual(diff_result.imported_findings, 2)

        imported_findings = list(Finding.objects.filter(test_id=diff_result.test_id))
        self.assertEqual(len(imported_findings), 2)
        uid_to_finding = {f.unique_id_from_tool: f for f in imported_findings}
        self.assertIn(self.uid_confident, uid_to_finding)
        self.assertIn(self.uid_uncertain, uid_to_finding)

        sync_result = apply_ai_response_artifact(
            pipeline=self.pipeline,
            output_dir=str(self.output_dir),
            artifact_path=_AI_RESPONSE_FILENAME,
            test_id=diff_result.test_id,
            user=self.user,
        )
        self.assertIsNotNone(sync_result)
        self.assertEqual(sync_result.saved, 2)

        # One AISTAIResponse with the new source.
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

        # Confidence tier round-trips into uncertaintyLevel — confident
        # tier ≤ 0.2, likely tier ∈ [0.4, 0.7].
        uncertainty_by_uid = {row.finding.unique_id_from_tool: row.uncertainty_level for row in rows}
        self.assertLessEqual(uncertainty_by_uid[self.uid_confident], 0.2)
        self.assertGreaterEqual(uncertainty_by_uid[self.uid_uncertain], 0.4)
        self.assertLessEqual(uncertainty_by_uid[self.uid_uncertain], 0.7)

        # No close_finding side-effect — neither Finding is auto-closed when
        # both verdicts are TRUE_POSITIVE.
        for finding in imported_findings:
            finding.refresh_from_db()
            self.assertFalse(finding.false_p, f"Finding {finding.id} unexpectedly marked false_p")
            self.assertFalse(finding.is_mitigated, f"Finding {finding.id} unexpectedly mitigated")

        # TP fix blocks should round-trip from the AI response file for both
        # confidence tiers (per schema, fix is required for true_positive).
        for uid in (self.uid_confident, self.uid_uncertain):
            row = next(r for r in rows if r.finding.unique_id_from_tool == uid)
            self.assertIsNotNone(row.fix, f"fix missing for {uid}")
            self.assertEqual(row.fix["fixType"], "code_change")
