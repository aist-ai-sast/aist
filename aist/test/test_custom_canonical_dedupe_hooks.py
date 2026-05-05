from __future__ import annotations

from unittest.mock import PropertyMock, patch

from django.conf import settings
from django.test import override_settings
from django.utils import timezone
from dojo.finding.deduplication import dedupe_batch_of_findings, do_dedupe_finding_task_internal
from dojo.models import Engagement, Finding, Test, Test_Type

from aist.dedupe.custom import AIST_DEDUPE_AUTO_TAG, AIST_DEDUPE_CANDIDATE_TAG
from aist.test.test_api import AISTApiBase


@override_settings(
    FINDING_DEDUPE_METHOD="aist.dedupe.custom.custom_dedupe_finding",
    FINDING_DEDUPE_BATCH_METHOD="aist.dedupe.custom.custom_dedupe_batch",
)
class CustomCanonicalDedupeHookTests(AISTApiBase):
    def _create_test(self, scan_type: str) -> Test:
        test_type, _ = Test_Type.objects.get_or_create(name=scan_type)
        engagement = Engagement.objects.create(
            name=f"eng-{scan_type}-{timezone.now().timestamp()}",
            target_start=timezone.now(),
            target_end=timezone.now(),
            product=self.product,
        )
        return Test.objects.create(
            engagement=engagement,
            target_start=timezone.now(),
            target_end=timezone.now(),
            test_type=test_type,
        )

    def _create_finding(
        self,
        *,
        test: Test,
        title: str,
        vuln_id: str,
        file_path: str,
        line: int,
        cwe: int | None = None,
        hash_code: str | None = None,
    ) -> Finding:
        return Finding.objects.create(
            test=test,
            title=title,
            severity="High",
            date=timezone.now(),
            reporter=self.user,
            vuln_id_from_tool=vuln_id,
            file_path=file_path,
            line=line,
            cwe=cwe,
            hash_code=hash_code,
        )

    def test_batch_hook_marks_cross_test_duplicate(self):
        original_test = self._create_test("Semgrep JSON Report")
        imported_test = self._create_test("Snyk Code Scan")
        original = self._create_finding(
            test=original_test,
            title="SSL verification disabled",
            vuln_id="python.lang.security.audit.ssl-no-verify",
            file_path="src/net.py",
            line=12,
            cwe=295,
        )
        imported = self._create_finding(
            test=imported_test,
            title="SSL verify false",
            vuln_id="python/SSLVerificationBypassed",
            file_path="src/net.py",
            line=12,
            cwe=295,
        )

        dedupe_batch_of_findings([imported])
        imported.refresh_from_db()

        self.assertTrue(imported.duplicate)
        self.assertEqual(imported.duplicate_finding_id, original.id)
        self.assertIn(AIST_DEDUPE_AUTO_TAG, set(imported.tags.values_list("name", flat=True)))
        self.assertNotIn(AIST_DEDUPE_CANDIDATE_TAG, set(imported.tags.values_list("name", flat=True)))

    def test_batch_hook_auto_matches_score_two(self):
        original_test = self._create_test("Semgrep JSON Report")
        imported_test = self._create_test("Bearer CLI")
        _ = self._create_finding(
            test=original_test,
            title="Custom issue in endpoint",
            vuln_id="custom_rule_x",
            file_path="app/views.py",
            line=88,
        )
        imported = self._create_finding(
            test=imported_test,
            title="Another custom issue",
            vuln_id="custom_rule_x",
            file_path="app/views.py",
            line=88,
        )

        dedupe_batch_of_findings([imported])
        imported.refresh_from_db()

        self.assertTrue(imported.duplicate)
        self.assertIn(AIST_DEDUPE_AUTO_TAG, set(imported.tags.values_list("name", flat=True)))
        self.assertNotIn(AIST_DEDUPE_CANDIDATE_TAG, set(imported.tags.values_list("name", flat=True)))

    def test_single_hook_marks_duplicate(self):
        original_test = self._create_test("Semgrep JSON Report")
        imported_test = self._create_test("Snyk Code Scan")
        original = self._create_finding(
            test=original_test,
            title="SQL injection",
            vuln_id="python/sql-injection",
            file_path="src/db.py",
            line=5,
            cwe=89,
        )
        imported = self._create_finding(
            test=imported_test,
            title="SQL injection",
            vuln_id="python/sql-injection",
            file_path="src/db.py",
            line=5,
            cwe=89,
            hash_code="single-custom-hook",
        )

        do_dedupe_finding_task_internal(imported)
        imported.refresh_from_db()

        self.assertTrue(imported.duplicate)
        self.assertEqual(imported.duplicate_finding_id, original.id)

    def test_non_supported_scan_type_uses_default_fallback(self):
        test = self._create_test("Custom Scanner")

        _ = self._create_finding(
            test=test,
            title="Legacy 1",
            vuln_id="legacy-a",
            file_path="src/legacy.py",
            line=1,
            hash_code="fallback-hash",
        )
        imported = self._create_finding(
            test=test,
            title="Legacy 2",
            vuln_id="legacy-b",
            file_path="src/legacy.py",
            line=2,
            hash_code="fallback-hash",
        )

        with patch.object(
            Test,
            "deduplication_algorithm",
            new_callable=PropertyMock,
            return_value=settings.DEDUPE_ALGO_HASH_CODE,
        ):
            dedupe_batch_of_findings([imported])
        imported.refresh_from_db()

        self.assertTrue(imported.duplicate)

    def test_supported_scan_type_with_line_zero_uses_fallback_hash_dedupe(self):
        test = self._create_test("Semgrep JSON Report")

        _ = self._create_finding(
            test=test,
            title="No Use Weak Random Number Generator",
            vuln_id="no_use_weak_random_number_generator",
            file_path="cloud/cms/static/tinymce/js/tinymce/tinymce.min.js",
            line=0,
            cwe=0,
            hash_code="9f8310b959cdf917dcfe318b85ece5cc708c64a277a093b92f485c855728aa8b",
        )
        imported = self._create_finding(
            test=test,
            title="No Use Weak Random Number Generator",
            vuln_id="no_use_weak_random_number_generator",
            file_path="cloud/cms/static/tinymce/js/tinymce/tinymce.min.js",
            line=0,
            cwe=0,
            hash_code="9f8310b959cdf917dcfe318b85ece5cc708c64a277a093b92f485c855728aa8b",
        )

        with patch.object(
            Test,
            "deduplication_algorithm",
            new_callable=PropertyMock,
            return_value=settings.DEDUPE_ALGO_UNIQUE_ID_FROM_TOOL_OR_HASH_CODE,
        ):
            dedupe_batch_of_findings([imported])
        imported.refresh_from_db()

        self.assertTrue(imported.duplicate)

    def test_batch_hook_marks_duplicate_for_jwt_secret_cross_scanner_case(self):
        semgrep_test = self._create_test("Semgrep JSON Report")
        snyk_test = self._create_test("Snyk Code Scan")
        original = self._create_finding(
            test=semgrep_test,
            title="JWT token detected",
            vuln_id="generic_secrets_security_detected_jwt_token_detected_jwt_token",
            file_path="src/config.ts",
            line=122,
            cwe=321,
        )
        imported = self._create_finding(
            test=snyk_test,
            title="Hardcoded non-crypto secret",
            vuln_id="javascript_hardcodednoncryptosecret",
            file_path="src/config.ts",
            line=122,
            cwe=547,
        )

        dedupe_batch_of_findings([imported])
        imported.refresh_from_db()

        self.assertTrue(imported.duplicate)
        self.assertEqual(imported.duplicate_finding_id, original.id)
        self.assertIn(AIST_DEDUPE_AUTO_TAG, set(imported.tags.values_list("name", flat=True)))

    def test_batch_hook_marks_duplicate_for_claude_sql_cluster(self):
        # Cluster 1 reproduction — SQL injection at the same line, three
        # paraphrased titles produced by the LLM. Canonical dedupe collapses
        # them to one root + N duplicates.
        claude_test = self._create_test("Claude Diff Security")
        original = self._create_finding(
            test=claude_test,
            title="SQL Injection in user-controlled query",
            vuln_id="claude:89:cloud/storage/dao.go:204:sql_injection",
            file_path="cloud/storage/analytics_db_service/internal/dao/ch_tracks_dao.go",
            line=1020,
            cwe=89,
        )
        imported = self._create_finding(
            test=claude_test,
            title="Untrusted input concatenated into SQL query",
            vuln_id="claude:89:cloud/storage/dao.go:204:sql_injection",
            file_path="cloud/storage/analytics_db_service/internal/dao/ch_tracks_dao.go",
            line=1020,
            cwe=89,
        )

        dedupe_batch_of_findings([imported])
        imported.refresh_from_db()

        self.assertTrue(imported.duplicate)
        self.assertEqual(imported.duplicate_finding_id, original.id)
        self.assertIn(AIST_DEDUPE_AUTO_TAG, set(imported.tags.values_list("name", flat=True)))

    def test_batch_hook_marks_duplicate_across_claude_and_semgrep(self):
        # Cross-scanner regression guard: Semgrep and Claude both report a CORS
        # misconfiguration on the same (file, line, CWE).
        claude_test = self._create_test("Claude Full Security")
        semgrep_test = self._create_test("Semgrep JSON Report")
        original = self._create_finding(
            test=semgrep_test,
            title="CORS misconfiguration in proxy origin reflection",
            vuln_id="javascript_corsmisconfig",
            file_path="cloud/connectivity/cloud_connect/redirecting_proxy/internal/proxy/service.go",
            line=40,
            cwe=942,
        )
        imported = self._create_finding(
            test=claude_test,
            title="Permissive CORS Origin Reflection",
            vuln_id="claude:942:cloud/connectivity/proxy/service.go:8:cors_misconfig",
            file_path="cloud/connectivity/cloud_connect/redirecting_proxy/internal/proxy/service.go",
            line=40,
            cwe=942,
        )

        dedupe_batch_of_findings([imported])
        imported.refresh_from_db()

        self.assertTrue(imported.duplicate)
        self.assertEqual(imported.duplicate_finding_id, original.id)
        self.assertIn(AIST_DEDUPE_AUTO_TAG, set(imported.tags.values_list("name", flat=True)))

    def test_batch_hook_uses_previous_finding_as_root_when_duplicate_chain_is_broken(self):
        semgrep_test = self._create_test("Semgrep JSON Report")
        snyk_test = self._create_test("Snyk Code Scan")
        broken_historical = self._create_finding(
            test=semgrep_test,
            title="JWT token detected",
            vuln_id="generic_secrets_security_detected_jwt_token_detected_jwt_token",
            file_path="src/config.ts",
            line=122,
            cwe=321,
        )
        Finding.objects.filter(id=broken_historical.id).update(duplicate=True, duplicate_finding=None)
        broken_historical.refresh_from_db()

        imported = self._create_finding(
            test=snyk_test,
            title="Hardcoded non-crypto secret",
            vuln_id="javascript_hardcodednoncryptosecret",
            file_path="src/config.ts",
            line=122,
            cwe=547,
        )

        dedupe_batch_of_findings([imported])
        imported.refresh_from_db()

        self.assertTrue(imported.duplicate)
        self.assertEqual(imported.duplicate_finding_id, broken_historical.id)
        self.assertIn(AIST_DEDUPE_AUTO_TAG, set(imported.tags.values_list("name", flat=True)))
