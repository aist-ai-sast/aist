"""
Unit tests for the Claude analyzer parser registration.

The Claude agent-bridge analyzers (claude-diff-security, claude-full-security)
register dedicated parsers that subclass ``GenericParser`` so the imported
``Test`` carries a Claude-specific ``test_type``. The parser stabilizes each
finding's ``vuln_id_from_tool`` to a deterministic token of the form
``claude:{cwe}:{normalized_file_path}:{line_bucket}:{family}`` so the standard
DefectDojo fallback hash dedup converges across runs even when the LLM
paraphrases the title or anchors to a slightly drifted line.
"""
from __future__ import annotations

import io
import json
from dataclasses import dataclass

from django.test import SimpleTestCase
from dojo.tools import factory

from aist.parser_overrides import (
    CLAUDE_DIFF_SECURITY_SCAN_TYPE,
    CLAUDE_FULL_SECURITY_SCAN_TYPE,
    CLAUDE_INTAKE_DIFF_SCAN_TYPE,
    CLAUDE_INTAKE_REVIEW_SCAN_TYPE,
    CLAUDE_LINE_BUCKET_SIZE,
    ClaudeDiffSecurityParser,
    ClaudeFullSecurityParser,
    ClaudeIntakeDiffParser,
    ClaudeIntakeReviewParser,
    build_claude_vuln_id_from_tool,
    install_claude_parsers,
)


class BuildClaudeVulnIdFromToolTests(SimpleTestCase):
    def test_token_is_stable_across_paraphrased_titles(self):
        # Both titles must convey the same family signal — paraphrasing that
        # keeps the family-keyword (here "SQL injection" / "SQLi") yields the
        # same token. A paraphrase that drops the family keyword entirely is
        # outside this guarantee.
        first = build_claude_vuln_id_from_tool(
            cwe=89,
            file_path="cloud/storage/dao/ch_tracks_dao.go",
            line=1020,
            title="SQL Injection in user-controlled query",
        )
        second = build_claude_vuln_id_from_tool(
            cwe=89,
            file_path="cloud/storage/dao/ch_tracks_dao.go",
            line=1020,
            title="Untrusted SQLi sink: query concatenated from request body",
        )
        self.assertEqual(first, second)
        self.assertTrue(first.startswith("claude:89:cloud/storage/dao/ch_tracks_dao.go:"))
        self.assertTrue(first.endswith(":sql_injection"))

    def test_token_is_stable_within_line_bucket(self):
        # Lines that fall into the same ``line // CLAUDE_LINE_BUCKET_SIZE``
        # bucket produce one token. The LLM may shift its anchor inside a
        # block by a few lines across runs without breaking dedup.
        in_bucket = build_claude_vuln_id_from_tool(
            cwe=942, file_path="src/proxy.go", line=40, title="CORS misconfiguration",
        )
        also_in_bucket = build_claude_vuln_id_from_tool(
            cwe=942, file_path="src/proxy.go",
            line=40 + (CLAUDE_LINE_BUCKET_SIZE - 1),
            title="Permissive CORS Origin Reflection",
        )
        self.assertEqual(in_bucket, also_in_bucket)

    def test_token_diverges_across_bucket_boundary(self):
        below = build_claude_vuln_id_from_tool(
            cwe=89, file_path="src/db.go", line=99, title="SQL injection",
        )
        above = build_claude_vuln_id_from_tool(
            cwe=89, file_path="src/db.go", line=100, title="SQL injection",
        )
        self.assertNotEqual(below, above)

    def test_token_normalizes_path_separators_and_case(self):
        a = build_claude_vuln_id_from_tool(
            cwe=89, file_path=r"SRC\\App/Main.go", line=10, title="SQL injection",
        )
        b = build_claude_vuln_id_from_tool(
            cwe=89, file_path="src/app/main.go", line=10, title="SQL injection",
        )
        self.assertEqual(a, b)

    def test_token_falls_back_to_unknown_family_when_inference_fails(self):
        token = build_claude_vuln_id_from_tool(
            cwe=0, file_path="src/foo.go", line=10, title="completely unrecognized issue text",
        )
        self.assertTrue(token.endswith(":unknown"))
        self.assertTrue(token.startswith("claude:0:"))


# ---------------------------------------------------------------------------
# Parser registration & end-to-end stabilization through the Claude parser
# ---------------------------------------------------------------------------


@dataclass
class _NamedFile:
    name: str
    payload: bytes

    def read(self):
        return self.payload


def _claude_findings_payload() -> bytes:
    return json.dumps({
        "findings": [
            {
                "title": "SQL Injection in user-controlled query",
                "severity": "High",
                "description": "evidence",
                "file_path": "cloud/storage/dao/ch_tracks_dao.go",
                "line": 1020,
                "cwe": 89,
                "mitigation": "use parameterized queries",
                "static_finding": True,
                "vuln_id_from_tool": "abcdef0123456789abcdef0123456789",
                "unique_id_from_tool": "1111111111111111aaaa1111111111aa",
            },
            {
                "title": "endpoint exposed without authentication",
                "severity": "High",
                "description": "evidence",
                "file_path": "src/foo.go",
                "line": 12,
                "mitigation": "add auth",
                "static_finding": True,
                "vuln_id_from_tool": "feedface00000000feedface00000000",
                "unique_id_from_tool": "2222222222222222bbbb2222222222bb",
            },
        ],
    }).encode("utf-8")


class ClaudeParserRegistrationTests(SimpleTestCase):
    def test_install_registers_both_parsers(self):
        install_claude_parsers()
        self.assertIsInstance(
            factory.PARSERS.get(CLAUDE_DIFF_SECURITY_SCAN_TYPE),
            ClaudeDiffSecurityParser,
        )
        self.assertIsInstance(
            factory.PARSERS.get(CLAUDE_FULL_SECURITY_SCAN_TYPE),
            ClaudeFullSecurityParser,
        )

    def test_get_scan_types_advertises_unique_test_type(self):
        self.assertEqual(
            ClaudeDiffSecurityParser().get_scan_types(),
            [CLAUDE_DIFF_SECURITY_SCAN_TYPE],
        )
        self.assertEqual(
            ClaudeFullSecurityParser().get_scan_types(),
            [CLAUDE_FULL_SECURITY_SCAN_TYPE],
        )

    def test_install_registers_intake_parsers(self):
        install_claude_parsers()
        self.assertIsInstance(
            factory.PARSERS.get(CLAUDE_INTAKE_REVIEW_SCAN_TYPE),
            ClaudeIntakeReviewParser,
        )
        self.assertIsInstance(
            factory.PARSERS.get(CLAUDE_INTAKE_DIFF_SCAN_TYPE),
            ClaudeIntakeDiffParser,
        )

    def test_intake_parsers_advertise_unique_test_types(self):
        self.assertEqual(
            ClaudeIntakeReviewParser().get_scan_types(),
            [CLAUDE_INTAKE_REVIEW_SCAN_TYPE],
        )
        self.assertEqual(
            ClaudeIntakeDiffParser().get_scan_types(),
            [CLAUDE_INTAKE_DIFF_SCAN_TYPE],
        )


class ClaudeParserStabilizationTests(SimpleTestCase):
    def _parse(self) -> list:
        scan = io.BytesIO(_claude_findings_payload())
        scan.name = "claude-diff-security_result.json"
        tests = ClaudeDiffSecurityParser().get_tests(CLAUDE_DIFF_SECURITY_SCAN_TYPE, scan)
        self.assertEqual(len(tests), 1)
        return tests[0].findings

    def test_parser_rewrites_vuln_id_to_deterministic_token(self):
        findings = self._parse()
        sql = findings[0]
        self.assertTrue(sql.vuln_id_from_tool.startswith("claude:89:"))
        self.assertTrue(sql.vuln_id_from_tool.endswith(":sql_injection"))
        # unique_id_from_tool is not touched — the AI response sidecar relies
        # on it as the match key.
        self.assertEqual(sql.unique_id_from_tool, "1111111111111111aaaa1111111111aa")

    def test_parser_backfills_cwe_from_inferred_family(self):
        findings = self._parse()
        auth_finding = findings[1]
        # Inferred family MISSING_AUTHENTICATION → CWE 306.
        self.assertEqual(auth_finding.cwe, 306)
        self.assertTrue(auth_finding.vuln_id_from_tool.startswith("claude:306:"))
        self.assertTrue(auth_finding.vuln_id_from_tool.endswith(":missing_authentication"))
