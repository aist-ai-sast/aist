from __future__ import annotations

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
        test.deduplication_algorithm = settings.DEDUPE_ALGO_HASH_CODE
        test.save(update_fields=["deduplication_algorithm"])

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

        dedupe_batch_of_findings([imported])
        imported.refresh_from_db()

        self.assertTrue(imported.duplicate)

    def test_supported_scan_type_with_line_zero_uses_fallback_hash_dedupe(self):
        test = self._create_test("Semgrep JSON Report")
        test.deduplication_algorithm = settings.DEDUPE_ALGO_UNIQUE_ID_FROM_TOOL_OR_HASH_CODE
        test.save(update_fields=["deduplication_algorithm"])

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

        dedupe_batch_of_findings([imported])
        imported.refresh_from_db()

        self.assertTrue(imported.duplicate)
