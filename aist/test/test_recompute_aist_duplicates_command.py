from __future__ import annotations

import json
from io import StringIO

from django.core.management import call_command
from django.utils import timezone
from dojo.models import Engagement, Finding, Test, Test_Type

from aist.dedupe.custom import AIST_DEDUPE_CANDIDATE_TAG
from aist.models import AISTPipeline, AISTStatus
from aist.test.test_api import AISTApiBase


class RecomputeAistDuplicatesCommandTests(AISTApiBase):
    def _create_finding(
        self,
        *,
        scan_type: str,
        title: str,
        vuln_id: str,
        file_path: str,
        line: int,
        cwe: int | None = None,
        unique_id: str = "",
        test: Test | None = None,
    ) -> Finding:
        if test is None:
            engagement = Engagement.objects.create(
                name=f"eng-{scan_type}-{timezone.now().timestamp()}",
                target_start=timezone.now(),
                target_end=timezone.now(),
                product=self.product,
            )
            test_type, _ = Test_Type.objects.get_or_create(name=scan_type)
            test = Test.objects.create(
                engagement=engagement,
                target_start=timezone.now(),
                target_end=timezone.now(),
                test_type=test_type,
            )
        return Finding.objects.create(
            test=test,
            title=title,
            severity="High",
            date=timezone.now(),
            reporter=self.user,
            vuln_id_from_tool=vuln_id,
            unique_id_from_tool=unique_id,
            file_path=file_path,
            line=line,
            cwe=cwe,
        )

    def test_dry_run_does_not_modify_duplicates(self):
        original = self._create_finding(
            scan_type="Semgrep JSON Report",
            title="SSL verification disabled",
            vuln_id="python.lang.security.audit.ssl-no-verify",
            file_path="src/net.py",
            line=12,
            cwe=295,
        )
        duplicate = self._create_finding(
            scan_type="Snyk Code Scan",
            title="SSL verify false",
            vuln_id="python/SSLVerificationBypassed",
            file_path="src/net.py",
            line=12,
            cwe=295,
        )

        out = StringIO()
        call_command("recompute_aist_duplicates", "--dry-run", "--explain-json", stdout=out)
        duplicate.refresh_from_db()

        self.assertFalse(duplicate.duplicate)
        self.assertIsNone(duplicate.duplicate_finding_id)
        output = out.getvalue()
        self.assertIn("auto_duplicates=1", output)
        self.assertIn("duplicate_group", output)
        self.assertIn(f"finding_id={duplicate.id}", output)
        explanations = [json.loads(line) for line in output.splitlines() if line.startswith("{")]
        duplicate_explanation = next(row for row in explanations if row["finding_id"] == duplicate.id)
        self.assertEqual(duplicate_explanation["verdict"], "duplicate")
        self.assertEqual(duplicate_explanation["location_strength"], "source_exact")
        self.assertIn("evidence_contributions", duplicate_explanation)
        self.assertIn("db_candidates", duplicate_explanation)
        self.assertIn("duration_ms", duplicate_explanation)
        self.assertEqual(duplicate_explanation["severity"], "High")
        self.assertEqual(duplicate_explanation["root_severity"], "High")
        self.assertFalse(duplicate_explanation["severity_mismatch"])
        self.assertTrue(Finding.objects.filter(id=original.id).exists())

    def test_apply_cross_scanner_duplicates_and_negative_case(self):
        semgrep_ssl = self._create_finding(
            scan_type="Semgrep JSON Report",
            title="SSL verification disabled",
            vuln_id="python.lang.security.audit.ssl-no-verify",
            file_path="src/ssl.py",
            line=15,
            cwe=295,
        )
        snyk_ssl = self._create_finding(
            scan_type="Snyk Code Scan",
            title="Insecure SSL verify false",
            vuln_id="python/SSLVerificationBypassed",
            file_path="src/ssl.py",
            line=15,
            cwe=295,
        )

        semgrep_secret = self._create_finding(
            scan_type="Semgrep JSON Report",
            title="Detected private key",
            vuln_id="generic.secrets.security.detected-private-key.detected-private-key",
            file_path="keys/service.key",
            line=1,
            cwe=321,
        )
        horusec_secret = self._create_finding(
            scan_type="Horusec Scan",
            title="Possible Vulnerability Detected: Private key exposed",
            vuln_id="",
            file_path="keys/service.key",
            line=1,
            cwe=None,
        )

        semgrep_path = self._create_finding(
            scan_type="Semgrep JSON Report",
            title="Path traversal in file download",
            vuln_id="python.flask.security.path-traversal",
            file_path="api/files.py",
            line=40,
            cwe=22,
        )
        bearer_path = self._create_finding(
            scan_type="Bearer CLI",
            title="Path Traversal vulnerability",
            vuln_id="path-traversal",
            file_path="api/files.py",
            line=40,
            cwe=22,
        )

        negative = self._create_finding(
            scan_type="Semgrep JSON Report",
            title="Open redirect",
            vuln_id="open-redirect-rule",
            file_path="api/files.py",
            line=40,
            cwe=601,
        )

        out = StringIO()
        call_command("recompute_aist_duplicates", "--apply", "--product-id", str(self.product.id), stdout=out)

        snyk_ssl.refresh_from_db()
        horusec_secret.refresh_from_db()
        bearer_path.refresh_from_db()
        negative.refresh_from_db()

        self.assertTrue(snyk_ssl.duplicate)
        self.assertEqual(snyk_ssl.duplicate_finding_id, semgrep_ssl.id)
        self.assertTrue(horusec_secret.duplicate)
        self.assertEqual(horusec_secret.duplicate_finding_id, semgrep_secret.id)
        self.assertTrue(bearer_path.duplicate)
        self.assertEqual(bearer_path.duplicate_finding_id, semgrep_path.id)
        self.assertFalse(negative.duplicate)
        self.assertIn("auto_duplicates=3", out.getvalue())

    def test_apply_auto_matches_rule_only_match(self):
        first = self._create_finding(
            scan_type="Semgrep JSON Report",
            title="Custom issue in endpoint",
            vuln_id="custom_rule_x",
            file_path="app/views.py",
            line=88,
            cwe=None,
        )
        second = self._create_finding(
            scan_type="Bearer CLI",
            title="Another custom issue",
            vuln_id="custom_rule_x",
            file_path="app/views.py",
            line=88,
            cwe=None,
        )

        out = StringIO()
        call_command("recompute_aist_duplicates", "--apply", stdout=out)
        first.refresh_from_db()
        second.refresh_from_db()

        self.assertTrue(second.duplicate)
        self.assertEqual(second.duplicate_finding_id, first.id)
        self.assertIn("auto_duplicates=1", out.getvalue())

    def test_apply_candidates_promotes_candidate_to_duplicate(self):
        first = self._create_finding(
            scan_type="Semgrep JSON Report",
            title="Custom issue in endpoint",
            vuln_id="custom_rule_y",
            file_path="app/promote.py",
            line=77,
            cwe=None,
        )
        second = self._create_finding(
            scan_type="Bearer CLI",
            title="Another custom issue",
            vuln_id="custom_rule_y",
            file_path="app/promote.py",
            line=77,
            cwe=None,
        )

        out = StringIO()
        call_command("recompute_aist_duplicates", "--apply", "--apply-candidates", stdout=out)
        second.refresh_from_db()
        output = out.getvalue()

        self.assertTrue(second.duplicate)
        self.assertEqual(second.duplicate_finding_id, first.id)
        self.assertIn("promoted_candidates=0", output)
        self.assertIn("applied_duplicates=1", output)

    def test_apply_candidates_implies_apply(self):
        first = self._create_finding(
            scan_type="Semgrep JSON Report",
            title="Custom issue in endpoint",
            vuln_id="custom_rule_z",
            file_path="app/implies.py",
            line=99,
            cwe=None,
        )
        second = self._create_finding(
            scan_type="Bearer CLI",
            title="Another custom issue",
            vuln_id="custom_rule_z",
            file_path="app/implies.py",
            line=99,
            cwe=None,
        )

        out = StringIO()
        call_command("recompute_aist_duplicates", "--apply-candidates", stdout=out)
        second.refresh_from_db()

        self.assertTrue(second.duplicate)
        self.assertEqual(second.duplicate_finding_id, first.id)
        self.assertIn("mode=apply", out.getvalue())

    def test_pipeline_filter_limits_scope(self):
        test_type = Test_Type.objects.create(name="Semgrep JSON Report")
        engagement = Engagement.objects.create(
            name="eng-pipeline",
            target_start=timezone.now(),
            target_end=timezone.now(),
            product=self.product,
        )
        test_in_pipeline = Test.objects.create(
            engagement=engagement,
            target_start=timezone.now(),
            target_end=timezone.now(),
            test_type=test_type,
        )
        test_outside = Test.objects.create(
            engagement=engagement,
            target_start=timezone.now(),
            target_end=timezone.now(),
            test_type=test_type,
        )
        pipeline = AISTPipeline.objects.create(
            id="recompute-pipeline-filter",
            project=self.project,
            project_version=self.pv,
            status=AISTStatus.FINISHED,
        )
        pipeline.tests.add(test_in_pipeline)

        _ = self._create_finding(
            scan_type="Semgrep JSON Report",
            title="SQL injection",
            vuln_id="sqli-a",
            file_path="api/db.py",
            line=9,
            cwe=89,
            test=test_in_pipeline,
        )
        duplicate = self._create_finding(
            scan_type="Snyk Code Scan",
            title="SQL injection",
            vuln_id="sqli-b",
            file_path="api/db.py",
            line=9,
            cwe=89,
            test=test_in_pipeline,
        )
        outside = self._create_finding(
            scan_type="Snyk Code Scan",
            title="SQL injection",
            vuln_id="sqli-b",
            file_path="api/db.py",
            line=9,
            cwe=89,
            test=test_outside,
        )

        call_command("recompute_aist_duplicates", "--apply", "--pipeline-id", pipeline.id)
        duplicate.refresh_from_db()
        outside.refresh_from_db()

        self.assertTrue(duplicate.duplicate)
        self.assertFalse(outside.duplicate)

    def test_pipeline_filter_recomputes_exact_uid_against_historical_scope(self):
        root = self._create_finding(
            scan_type="Semgrep JSON Report",
            title="Original SQL injection",
            vuln_id="semgrep-rule-v1",
            unique_id="semgrep-result-91",
            file_path="src/old_query.py",
            line=11,
            cwe=89,
        )
        unsafe_root = self._create_finding(
            scan_type="Semgrep JSON Report",
            title="Original command injection",
            vuln_id="semgrep-command-v1",
            unique_id="semgrep-result-92",
            file_path="src/old_process.py",
            line=18,
            cwe=78,
        )
        unsafe_root.severity = "Low"
        unsafe_root.save(update_fields=["severity"])
        imported = self._create_finding(
            scan_type="Semgrep JSON Report",
            title="Moved SQL injection",
            vuln_id="semgrep-rule-v2",
            unique_id="semgrep-result-91",
            file_path="src/new_query.py",
            line=72,
            cwe=20,
        )
        repeated_test = imported.test
        unsafe_imported = self._create_finding(
            scan_type="Semgrep JSON Report",
            title="Moved command injection",
            vuln_id="semgrep-command-v2",
            unique_id="semgrep-result-92",
            file_path="src/new_process.py",
            line=81,
            cwe=77,
            test=repeated_test,
        )
        unsafe_imported.severity = "Critical"
        unsafe_imported.save(update_fields=["severity"])
        pipeline = AISTPipeline.objects.create(
            id="recompute-exact-pipeline-filter",
            project=self.project,
            project_version=self.pv,
            status=AISTStatus.FINISHED,
        )
        pipeline.tests.add(repeated_test)

        dry_run_out = StringIO()
        call_command(
            "recompute_aist_duplicates",
            "--dry-run",
            "--explain-json",
            "--pipeline-id",
            pipeline.id,
            stdout=dry_run_out,
        )
        imported.refresh_from_db()
        unsafe_imported.refresh_from_db()

        self.assertFalse(imported.duplicate)
        self.assertFalse(unsafe_imported.duplicate)
        self.assertIn("processed=2 exact_duplicates=1", dry_run_out.getvalue())
        explanations = [
            json.loads(line)
            for line in dry_run_out.getvalue().splitlines()
            if line.startswith("{")
        ]
        unsafe_explanation = next(
            row for row in explanations if row["finding_id"] == unsafe_imported.id
        )
        self.assertEqual(unsafe_explanation["verdict"], "candidate")
        self.assertEqual(unsafe_explanation["source"], "unique_id_from_tool")
        self.assertEqual(unsafe_explanation["root_id"], unsafe_root.id)
        self.assertEqual(unsafe_explanation["root_severity"], "Low")
        self.assertTrue(unsafe_explanation["severity_mismatch"])

        apply_out = StringIO()
        call_command(
            "recompute_aist_duplicates",
            "--apply-candidates",
            "--pipeline-id",
            pipeline.id,
            stdout=apply_out,
        )
        imported.refresh_from_db()
        unsafe_imported.refresh_from_db()

        self.assertTrue(imported.duplicate)
        self.assertEqual(imported.duplicate_finding_id, root.id)
        self.assertFalse(unsafe_imported.duplicate)
        self.assertTrue(unsafe_imported.active)
        self.assertIn(
            AIST_DEDUPE_CANDIDATE_TAG,
            set(unsafe_imported.tags.values_list("name", flat=True)),
        )
        self.assertIn("processed=2 exact_duplicates=1", apply_out.getvalue())
        self.assertIn("promoted_candidates=0", apply_out.getvalue())

    def test_line_zero_uses_fallback_hash_dedupe(self):
        first = self._create_finding(
            scan_type="Semgrep JSON Report",
            title="No Use Weak Random Number Generator",
            vuln_id="no_use_weak_random_number_generator",
            file_path="cloud/cms/static/tinymce/js/tinymce/tinymce.min.js",
            line=0,
            cwe=0,
        )
        second = self._create_finding(
            scan_type="Semgrep JSON Report",
            title="No Use Weak Random Number Generator",
            vuln_id="no_use_weak_random_number_generator",
            file_path="cloud/cms/static/tinymce/js/tinymce/tinymce.min.js",
            line=0,
            cwe=0,
            test=first.test,
        )
        first.hash_code = "9f8310b959cdf917dcfe318b85ece5cc708c64a277a093b92f485c855728aa8b"
        second.hash_code = "9f8310b959cdf917dcfe318b85ece5cc708c64a277a093b92f485c855728aa8b"
        first.save(update_fields=["hash_code"])
        second.save(update_fields=["hash_code"])

        out = StringIO()
        call_command("recompute_aist_duplicates", "--apply", stdout=out)
        second.refresh_from_db()

        self.assertTrue(second.duplicate)
        self.assertEqual(second.duplicate_finding_id, first.id)
        self.assertIn("auto_duplicates=1", out.getvalue())
