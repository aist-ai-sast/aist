"""
End-to-end test for the claude-diff-security analyzer's report-first flow.

The bridge invocation itself is covered separately by sast-pipeline tests. Here the bridge has already produced its
Generic Findings Import and AI response files; the shared flow in ``aist.test.agent_analyzer_import`` imports and
syncs them.
"""
from __future__ import annotations

from aist.parser_overrides import CLAUDE_DIFF_SECURITY_SCAN_TYPE
from aist.test.agent_analyzer_import import AgentAnalyzerImportAndSync
from aist.test.test_api import AISTApiBase

UID_CONFIDENT = "uid-confident-ssrf"
UID_UNCERTAIN = "uid-uncertain-statetx"

GFI_FINDINGS = [
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
        "unique_id_from_tool": UID_CONFIDENT,
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
        "unique_id_from_tool": UID_UNCERTAIN,
    },
]

AI_TRUE_POSITIVES = [
    {
        "uniqueIdFromTool": UID_CONFIDENT,
        "title": "SSRF",
        "reasoning": "## Verdict\nTP\n## Evidence\nx",
        "references": [
            "https://example.test/cwe-918",
        ],
        "epssScore": 0.05,
        "impactScore": 7.5,
        "exploitabilityScore": 4.0,
        "uncertaintyLevel": 0.1,
        "uncertaintySpread": 0.05,
        "exploitCodeMaturity": "",
        "fix": {
            "fixType": "code_change",
            "fixSummary": "Validate URL scheme and host before fetch",
            "diffAvailable": True,
            "diff": "-old\n+new\n",
            "codeAfter": None,
            "stepByStep": [
                "Step 1: validate scheme",
                "Step 2: allow-list hosts",
            ],
            "testingHint": None,
            "secretsManagement": None,
            "suppressionAnnotation": None,
        },
    },
    {
        "uniqueIdFromTool": UID_UNCERTAIN,
        "title": "Account activation reachable without registration token",
        "reasoning": "## Verdict\nTP\n## Evidence\nLookup widened to email-only.",
        "references": [],
        "epssScore": None,
        "impactScore": 8.0,
        "exploitabilityScore": 6.0,
        "uncertaintyLevel": 0.55,
        "uncertaintySpread": 0.1,
        "exploitCodeMaturity": "",
        "fix": {
            "fixType": "code_change",
            "fixSummary": "Restore proof-of-prior-state check on activation",
            "diffAvailable": True,
            "diff": "-email-only\n+token-bound\n",
            "codeAfter": None,
            "stepByStep": [
                "Step 1: require token from registration step",
            ],
            "testingHint": None,
            "secretsManagement": None,
            "suppressionAnnotation": None,
        },
    },
]


class ClaudeDiffSecurityE2ETests(AgentAnalyzerImportAndSync, AISTApiBase):

    ANALYZER_NAME = "claude-diff-security"
    SKILL_NAME = "aist-diff-security-review"
    SCAN_TYPE = CLAUDE_DIFF_SECURITY_SCAN_TYPE
    PIPELINE_ID = "pipe-diff-e2e"
    UID_CONFIDENT = UID_CONFIDENT
    UID_UNCERTAIN = UID_UNCERTAIN
    GFI_FINDINGS = GFI_FINDINGS
    AI_TRUE_POSITIVES = AI_TRUE_POSITIVES
