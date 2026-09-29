"""
Database-backed scenarios for evolution deduplication.

Reproduces the production flow: a scanner finding is closed as False Positive
(which also sets is_mitigated), the code shifts, and the next scan reports the
same line content at a different line number.
"""
from __future__ import annotations

from django.utils import timezone
from dojo.models import DojoMeta, Engagement, Finding, Test, Test_Type

from aist.dedupe.evolution import AIST_EVOLUTION_TAG, AIST_LHASH_META_NAME, run_evolution_dedup
from aist.test.test_api import AISTApiBase

LOGGER_LINE_HASH = "f4ffd0aff9a1b2c3"


class EvolutionDedupeReviewedAncestorTests(AISTApiBase):
    def _scan(self, name: str, scan_type: str = "Bearer CLI") -> Test:
        engagement = Engagement.objects.create(
            name=name,
            target_start=timezone.now(),
            target_end=timezone.now(),
            product=self.product,
        )
        test_type, _ = Test_Type.objects.get_or_create(name=scan_type)
        return Test.objects.create(
            engagement=engagement,
            target_start=timezone.now(),
            target_end=timezone.now(),
            test_type=test_type,
        )

    def _finding(self, test: Test, *, line: int, vuln_id: str = "python_lang_logger", **status) -> Finding:
        finding = Finding.objects.create(
            test=test,
            title="Leakage of Sensitive Information in Logger Message",
            severity="Medium",
            date=timezone.now(),
            reporter=self.user,
            vuln_id_from_tool=vuln_id,
            file_path="channel_partners/src/partners/services/sso_service.py",
            line=line,
            cwe=532,
            **status,
        )
        DojoMeta.objects.create(finding=finding, name=AIST_LHASH_META_NAME, value=LOGGER_LINE_HASH)
        return finding

    def _closed_as_false_positive(self, test: Test, *, line: int, vuln_id: str = "python_lang_logger") -> Finding:
        return self._finding(
            test,
            line=line,
            vuln_id=vuln_id,
            active=False,
            false_p=True,
            is_mitigated=True,
            mitigated=timezone.now(),
        )

    def test_shifted_rescan_inherits_false_positive_ancestor(self):
        previous_scan = self._scan("weekly scan 1")
        ancestor = self._closed_as_false_positive(previous_scan, line=1173)
        current_scan = self._scan("weekly scan 2")
        rescanned = self._finding(current_scan, line=1231)

        matched = run_evolution_dedup(pipeline_id="weekly-2", test_ids=[current_scan.id])

        rescanned.refresh_from_db()
        self.assertEqual(matched, 1)
        self.assertTrue(rescanned.duplicate)
        self.assertFalse(rescanned.active)
        self.assertEqual(rescanned.duplicate_finding_id, ancestor.id)
        self.assertIn(AIST_EVOLUTION_TAG, [tag.name for tag in rescanned.tags.all()])

    def test_legacy_slash_rule_id_still_matches_false_positive_ancestor(self):
        previous_scan = self._scan("legacy snyk scan", scan_type="Snyk Code Scan")
        ancestor = self._closed_as_false_positive(previous_scan, line=624, vuln_id="python/XSS")
        current_scan = self._scan("current snyk scan", scan_type="Snyk Code Scan")
        rescanned = self._finding(current_scan, line=624, vuln_id="python_xss")

        matched = run_evolution_dedup(pipeline_id="snyk-2", test_ids=[current_scan.id])

        rescanned.refresh_from_db()
        self.assertEqual(matched, 1)
        self.assertEqual(rescanned.duplicate_finding_id, ancestor.id)

    def test_backfill_of_older_scan_does_not_point_at_newer_copy(self):
        older_scan = self._scan("weekly scan 1")
        older_copy = self._closed_as_false_positive(older_scan, line=1173)
        newer_scan = self._scan("weekly scan 2")
        newer_copy = self._closed_as_false_positive(newer_scan, line=1231)

        matched = run_evolution_dedup(pipeline_id="weekly-1", test_ids=[older_scan.id])

        older_copy.refresh_from_db()
        self.assertEqual(matched, 0)
        self.assertFalse(older_copy.duplicate)

        matched = run_evolution_dedup(pipeline_id="weekly-2", test_ids=[newer_scan.id])

        newer_copy.refresh_from_db()
        self.assertEqual(matched, 1)
        self.assertEqual(newer_copy.duplicate_finding_id, older_copy.id)

    def test_fixed_ancestor_does_not_absorb_regression(self):
        previous_scan = self._scan("scan before fix")
        self._finding(previous_scan, line=1173, active=False, is_mitigated=True, mitigated=timezone.now())
        current_scan = self._scan("scan after regression")
        regression = self._finding(current_scan, line=1231)

        matched = run_evolution_dedup(pipeline_id="regression", test_ids=[current_scan.id])

        regression.refresh_from_db()
        self.assertEqual(matched, 0)
        self.assertFalse(regression.duplicate)
        self.assertTrue(regression.active)
