"""
End-to-end test for the claude-full-security analyzer's report-first flow.

It runs the same shared flow as ``test_diff_security_e2e.py`` (``aist.test.agent_analyzer_import``), so an import path
that grows an analyzer-name branch fails one of the two. The bridge invocation itself is covered by sast-pipeline tests.
"""
from __future__ import annotations

from aist.parser_overrides import CLAUDE_FULL_SECURITY_SCAN_TYPE
from aist.test.agent_analyzer_import import AgentAnalyzerImportAndSync
from aist.test.test_api import AISTApiBase

UID_CONFIDENT = "uid-full-confident-pathtraversal"
UID_UNCERTAIN = "uid-full-uncertain-tenant-isolation"

GFI_FINDINGS = [
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
        "unique_id_from_tool": UID_CONFIDENT,
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
        "unique_id_from_tool": UID_UNCERTAIN,
    },
]

AI_TRUE_POSITIVES = [
    {
        "uniqueIdFromTool": UID_CONFIDENT,
        "title": "Path traversal",
        "reasoning": "## Verdict\nTP\n## Evidence\nx",
        "references": [
            "https://example.test/cwe-22",
        ],
        "epssScore": 0.07,
        "impactScore": 7.0,
        "exploitabilityScore": 5.0,
        "uncertaintyLevel": 0.1,
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
        "uniqueIdFromTool": UID_UNCERTAIN,
        "title": "Tenant isolation gap on bulk export endpoint",
        "reasoning": "## Verdict\nTP\n## Evidence\nOwnership check is on object, not caller.",
        "references": [],
        "epssScore": None,
        "impactScore": 8.0,
        "exploitabilityScore": 5.0,
        "uncertaintyLevel": 0.55,
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
]


class ClaudeFullSecurityE2ETests(AgentAnalyzerImportAndSync, AISTApiBase):

    ANALYZER_NAME = "claude-full-security"
    SKILL_NAME = "aist-full-security-review"
    SCAN_TYPE = CLAUDE_FULL_SECURITY_SCAN_TYPE
    PIPELINE_ID = "pipe-full-e2e"
    UID_CONFIDENT = UID_CONFIDENT
    UID_UNCERTAIN = UID_UNCERTAIN
    GFI_FINDINGS = GFI_FINDINGS
    AI_TRUE_POSITIVES = AI_TRUE_POSITIVES
