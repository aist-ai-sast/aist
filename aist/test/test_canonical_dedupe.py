from __future__ import annotations

from dataclasses import dataclass

from django.test import SimpleTestCase

from aist.dedupe.canonical import (
    CanonicalFamily,
    MatchVerdict,
    finding_signature,
    infer_canonical_family,
    normalize_file_path,
    score_findings,
)


@dataclass
class DummyFinding:
    title: str
    vuln_id_from_tool: str
    file_path: str
    line: int
    cwe: int | None = None
    component_name: str = ""
    component_version: str = ""


class CanonicalDedupeTests(SimpleTestCase):
    def test_normalize_file_path(self):
        self.assertEqual(normalize_file_path(r"SRC\\App//Main.py"), "src/app/main.py")

    def test_family_inference(self):
        family = infer_canonical_family(
            vuln_id="generic.secrets.security.detected-private-key.detected-private-key",
            title="Detected private key",
        )
        self.assertEqual(family, CanonicalFamily.PRIVATE_KEY)

    def test_hard_gate_requires_same_file_and_line(self):
        left = DummyFinding(
            title="SQL injection",
            vuln_id_from_tool="python/sql-injection",
            file_path="src/a.py",
            line=10,
            cwe=89,
        )
        right = DummyFinding(
            title="SQL injection",
            vuln_id_from_tool="python/sql-injection",
            file_path="src/b.py",
            line=10,
            cwe=89,
        )
        match = score_findings(left, right)
        self.assertEqual(match.verdict, MatchVerdict.NO_MATCH)
        self.assertEqual(match.score, 0)

    def test_score_duplicate_when_family_and_cwe_match(self):
        left = DummyFinding(
            title="SSL verification disabled",
            vuln_id_from_tool="python.lang.security.audit.ssl-no-verify",
            file_path="app/client.py",
            line=22,
            cwe=295,
        )
        right = DummyFinding(
            title="Insecure SSL verify False",
            vuln_id_from_tool="python/SSLVerificationBypassed",
            file_path="app/client.py",
            line=22,
            cwe=295,
        )
        match = score_findings(left, right)
        self.assertTrue(match.is_duplicate)
        self.assertEqual(match.verdict, MatchVerdict.DUPLICATE)
        self.assertGreaterEqual(match.score, 5)

    def test_score_no_match_for_partial_family_only_match(self):
        left = DummyFinding(
            title="Path traversal in upload endpoint",
            vuln_id_from_tool="path_traversal_rule",
            file_path="api/upload.py",
            line=33,
            cwe=None,
        )
        right = DummyFinding(
            title="Directory traversal detected",
            vuln_id_from_tool="another_rule_name",
            file_path="api/upload.py",
            line=33,
            cwe=None,
        )
        match = score_findings(left, right)
        self.assertEqual(match.verdict, MatchVerdict.NO_MATCH)
        self.assertEqual(match.score, 0)

    def test_finding_signature_uses_family_cwe_fallback(self):
        finding = DummyFinding(
            title="Hardcoded secret in source",
            vuln_id_from_tool="",
            file_path="src/settings.py",
            line=5,
            cwe=None,
        )
        signature = finding_signature(finding)
        self.assertEqual(signature.family, CanonicalFamily.HARDCODED_SECRET)
        self.assertEqual(signature.cwe, 798)
