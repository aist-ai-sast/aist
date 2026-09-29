from __future__ import annotations

from dataclasses import dataclass

from django.test import SimpleTestCase

from aist.dedupe.canonical import (
    CanonicalFamily,
    MatchVerdict,
    finding_signature,
    infer_canonical_family,
    normalize_canonical_rule_key,
    normalize_file_path,
    score_findings,
    tokenize_title,
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

    def test_family_inference_for_semgrep_jwt_secret_and_snyk_noncrypto_secret(self):
        semgrep_family = infer_canonical_family(
            vuln_id="generic_secrets_security_detected_jwt_token_detected_jwt_token",
            title="",
        )
        snyk_family = infer_canonical_family(
            vuln_id="javascript_hardcodednoncryptosecret",
            title="",
        )
        self.assertEqual(semgrep_family, CanonicalFamily.HARDCODED_SECRET)
        self.assertEqual(snyk_family, CanonicalFamily.HARDCODED_SECRET)

    def test_normalize_canonical_rule_key_aliases_secret_rules_only(self):
        secret_alias = normalize_canonical_rule_key(
            family=CanonicalFamily.HARDCODED_SECRET,
            value="generic_secrets_security_detected_jwt_token_detected_jwt_token",
        )
        non_secret_rule = normalize_canonical_rule_key(
            family=CanonicalFamily.SQL_INJECTION,
            value="python/sql-injection",
        )

        self.assertEqual(secret_alias, "secret_jwt_or_noncrypto_hardcoded")
        self.assertEqual(non_secret_rule, "python_sql_injection")

    def test_family_inference_for_missing_authentication(self):
        family = infer_canonical_family(
            vuln_id="",
            title="Log Collector Read and Write Endpoints Have No Authentication",
        )
        self.assertEqual(family, CanonicalFamily.MISSING_AUTHENTICATION)

    def test_family_inference_for_cors_misconfig(self):
        family = infer_canonical_family(
            vuln_id="",
            title="Permissive CORS Origin Reflection in proxy service",
        )
        self.assertEqual(family, CanonicalFamily.CORS_MISCONFIG)

    def test_family_inference_for_ssrf(self):
        family = infer_canonical_family(
            vuln_id="",
            title="Server-side request forgery via webhook URL",
        )
        self.assertEqual(family, CanonicalFamily.SSRF)

    def test_family_inference_for_host_header_injection(self):
        family = infer_canonical_family(
            vuln_id="",
            title="Host header injection on redirect endpoint",
        )
        self.assertEqual(family, CanonicalFamily.HOST_HEADER_INJECTION)

    def test_score_duplicate_for_missing_authentication_cluster(self):
        # Cluster 3 reproduction: missing authentication on log_collector
        # service line 64 — paraphrased titles, identical CWE/file/line.
        left = DummyFinding(
            title="Log Collector Read and Write Endpoints Have No Authentication",
            vuln_id_from_tool="claude:306:cloud/infra/log_collector/service.go:12:missing_authentication",
            file_path="cloud/infra/log_collector/internal/log_collector/service.go",
            line=64,
            cwe=306,
        )
        right = DummyFinding(
            title="Log Collector Service Exposes Read/Write Endpoints Without Authentication",
            vuln_id_from_tool="claude:306:cloud/infra/log_collector/service.go:12:missing_authentication",
            file_path="cloud/infra/log_collector/internal/log_collector/service.go",
            line=64,
            cwe=306,
        )
        match = score_findings(left, right)
        self.assertEqual(match.verdict, MatchVerdict.DUPLICATE)

    def test_score_duplicate_for_cross_scanner_cors_cluster(self):
        # Cross-scanner regression guard: Snyk + Claude on the same file/line
        # both report CORS misconfig with CWE 942 → must remain DUPLICATE.
        snyk = DummyFinding(
            title="Cross-Origin Resource Sharing misconfiguration",
            vuln_id_from_tool="javascript_corsmisconfig",
            file_path="src/api.ts",
            line=22,
            cwe=942,
        )
        claude = DummyFinding(
            title="Permissive CORS Origin Reflection on API endpoint",
            vuln_id_from_tool="claude:942:src/api.ts:4:cors_misconfig",
            file_path="src/api.ts",
            line=22,
            cwe=942,
        )
        match = score_findings(snyk, claude)
        self.assertEqual(match.verdict, MatchVerdict.DUPLICATE)

    def test_title_token_overlap_alone_yields_candidate(self):
        # Title-only signal — same file/line, no CWE, family inferred to
        # UNKNOWN. Score ends at 1 → CANDIDATE (tag, no link).
        left = DummyFinding(
            title="Unsafe handling of customer payment workflow notifications",
            vuln_id_from_tool="rule_alpha",
            file_path="src/handler.go",
            line=15,
        )
        right = DummyFinding(
            title="Unsafe customer payment workflow notification handling logic",
            vuln_id_from_tool="rule_beta",
            file_path="src/handler.go",
            line=15,
        )
        match = score_findings(left, right)
        self.assertEqual(match.verdict, MatchVerdict.CANDIDATE)

    def test_tokenize_title_filters_stopwords_and_short_tokens(self):
        tokens = tokenize_title("The Service Exposes No Authentication In Endpoints")
        self.assertNotIn("the", tokens)
        self.assertNotIn("in", tokens)
        self.assertNotIn("service", tokens)
        self.assertNotIn("exposes", tokens)
        self.assertNotIn("no", tokens)
        self.assertNotIn("endpoints", tokens)
        self.assertIn("authentication", tokens)

    def test_score_duplicate_for_semgrep_and_horusec_postmessage_origin(self):
        # Horusec reports no CWE and words the rule differently from Semgrep.
        semgrep = DummyFinding(
            title="Insufficient Postmessage Origin Validation",
            vuln_id_from_tool=(
                "javascript_browser_security_insufficient_postmessage_origin_validation"
                "_insufficient_postmessage_origin_validation"
            ),
            file_path="front_end/experiments/monitoring/src/main.ts",
            line=74,
            cwe=345,
        )
        horusec = DummyFinding(
            title="Origins Should Be Verified During Cross-Origin Communication",
            vuln_id_from_tool="origins_should_be_verified_during_cross_origin_communication",
            file_path="front_end/experiments/monitoring/src/main.ts",
            line=74,
            cwe=0,
        )

        self.assertEqual(finding_signature(horusec).family, CanonicalFamily.POSTMESSAGE_ORIGIN)
        match = score_findings(semgrep, horusec)
        self.assertEqual(match.verdict, MatchVerdict.DUPLICATE)

    def test_score_duplicate_for_llm_committed_token_and_snyk_hardcoded_secret(self):
        # Same secret on the same line, worded by an LLM analyzer as a committed token.
        snyk = DummyFinding(
            title="Hardcoded Non-Cryptographic Secret",
            vuln_id_from_tool="python_hardcodednoncryptosecret",
            file_path="plugins/plugin-inventory/scripts/report.py",
            line=28,
            cwe=547,
        )
        claude = DummyFinding(
            title="GitLab Project Access Token With Write Scope Committed to the Repository",
            vuln_id_from_tool="claude:798:plugins/plugin-inventory/scripts/report.py:5:unknown",
            file_path="plugins/plugin-inventory/scripts/report.py",
            line=28,
            cwe=798,
        )

        self.assertEqual(finding_signature(claude).family, CanonicalFamily.HARDCODED_SECRET)
        match = score_findings(snyk, claude)
        self.assertEqual(match.verdict, MatchVerdict.DUPLICATE)

    def test_explicit_cwe_alone_does_not_assign_family(self):
        # A family match alone reaches the production auto-duplicate threshold, so a
        # CWE bucket must not stand in for a recognised rule or title.
        finding = DummyFinding(
            title="Publisher-Controlled Track Count Becomes an Unbounded Array Index",
            vuln_id_from_tool="claude:89:src/recorder.cpp:78:unknown",
            file_path="src/recorder.cpp",
            line=392,
            cwe=89,
        )
        self.assertEqual(finding_signature(finding).family, CanonicalFamily.UNKNOWN)

    def test_different_cookie_flags_on_same_line_do_not_match(self):
        # Secure (CWE-614) and HttpOnly (CWE-1004) are separate weaknesses on one set_cookie call.
        semgrep = DummyFinding(
            title="Django Secure Set Cookie",
            vuln_id_from_tool="python_django_security_audit_secure_cookies_django_secure_set_cookie",
            file_path="cloud/api/views/utils.py",
            line=196,
            cwe=614,
        )
        bearer = DummyFinding(
            title="Missing HTTP Only Option in Cookie Configuration",
            vuln_id_from_tool="python_django_cookie_missing_http_only",
            file_path="cloud/api/views/utils.py",
            line=196,
            cwe=1004,
        )
        self.assertEqual(score_findings(semgrep, bearer).verdict, MatchVerdict.NO_MATCH)

    def test_score_duplicate_for_cross_scanner_jwt_secret_variants(self):
        semgrep = DummyFinding(
            title="JWT token detected",
            vuln_id_from_tool="generic_secrets_security_detected_jwt_token_detected_jwt_token",
            file_path="src/config.ts",
            line=122,
            cwe=321,
        )
        snyk = DummyFinding(
            title="Hardcoded non-crypto secret",
            vuln_id_from_tool="javascript_hardcodednoncryptosecret",
            file_path="src/config.ts",
            line=122,
            cwe=547,
        )

        match = score_findings(semgrep, snyk)
        self.assertTrue(match.is_duplicate)
        self.assertEqual(match.verdict, MatchVerdict.DUPLICATE)
        self.assertGreaterEqual(match.score, 4)
