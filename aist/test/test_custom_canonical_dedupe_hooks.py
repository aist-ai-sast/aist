from __future__ import annotations

from unittest.mock import PropertyMock, patch

from django.conf import settings
from django.test import override_settings
from django.utils import timezone
from dojo.finding.deduplication import dedupe_batch_of_findings, do_dedupe_finding_task_internal
from dojo.models import Endpoint, Engagement, Finding, Test, Test_Type, Vulnerability_Id

from aist.dedupe.custom import AIST_DEDUPE_AUTO_TAG, AIST_DEDUPE_CANDIDATE_TAG
from aist.logging_transport import get_pipeline_log_path
from aist.models import AISTPipeline, Organization, PipelineExecutionType
from aist.test import dast_fixtures
from aist.test.test_api import AISTApiBase


@override_settings(
    FINDING_DEDUPE_METHOD="aist.dedupe.custom.custom_dedupe_finding",
    FINDING_DEDUPE_BATCH_METHOD="aist.dedupe.custom.custom_dedupe_batch",
)
class CustomCanonicalDedupeHookTests(AISTApiBase):
    def _dast_bindings(self):
        bindings = getattr(self, "_cached_dast_bindings", None)
        if bindings is not None:
            return bindings
        organization = Organization.objects.create(
            name="Canonical DAST dedupe organization",
            product_type=self.prod_type,
        )
        integration, _ = dast_fixtures.create_dast_integration(organization=organization)
        first_target, second_target = dast_fixtures.create_dast_targets(
            integration=integration,
            wires=(
                dast_fixtures.perimeter_target_wire("public-api"),
                dast_fixtures.perimeter_target_wire("partner-api"),
            ),
        )
        bindings = (
            dast_fixtures.create_dast_binding(project=self.project, target=first_target),
            dast_fixtures.create_dast_binding(project=self.project, target=second_target),
        )
        self._cached_dast_bindings = bindings
        return bindings

    def _create_dast_test(self, binding) -> Test:
        test = self._create_test("DAST Autonomous Scan")
        pipeline = AISTPipeline.objects.create(
            id=f"dedupe-dast-{test.id}",
            project=self.project,
            execution_type=PipelineExecutionType.MANUAL_IMPORT,
            dast_binding=binding,
        )
        pipeline.tests.add(test)
        return test

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
        unique_id: str = "",
        file_path: str = "",
        line: int | None = None,
        cwe: int | None = None,
        hash_code: str | None = None,
        component_name: str = "",
        component_version: str = "",
        service: str = "",
        parameter: str = "",
        dynamic_finding: bool = False,
        severity: str = "High",
    ) -> Finding:
        return Finding.objects.create(
            test=test,
            title=title,
            severity=severity,
            date=timezone.now(),
            reporter=self.user,
            vuln_id_from_tool=vuln_id,
            unique_id_from_tool=unique_id,
            file_path=file_path,
            line=line,
            cwe=cwe,
            hash_code=hash_code,
            component_name=component_name,
            component_version=component_version,
            service=service,
            param=parameter,
            dynamic_finding=dynamic_finding,
        )

    def _add_endpoint(self, finding: Finding, *, protocol: str, host: str, port: int, path: str = "") -> None:
        endpoint = Endpoint.objects.create(
            product=self.product,
            protocol=protocol,
            host=host,
            port=port,
            path=path or None,
        )
        finding.endpoints.add(endpoint)

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

    def test_sast_reimport_uses_exact_producer_identity_before_canonical_correlation(self):
        original_test = self._create_test("Semgrep JSON Report")
        repeated_test = self._create_test("Semgrep JSON Report")
        original = self._create_finding(
            test=original_test,
            title="Original Semgrep finding",
            vuln_id="semgrep-rule-v1",
            unique_id="semgrep-result-42",
            file_path="src/original.py",
            line=12,
            cwe=89,
        )
        imported = self._create_finding(
            test=repeated_test,
            title="Updated Semgrep finding",
            vuln_id="semgrep-rule-v2",
            unique_id="semgrep-result-42",
            file_path="src/moved.py",
            line=48,
            cwe=20,
        )

        dedupe_batch_of_findings([imported])
        imported.refresh_from_db()

        self.assertTrue(imported.duplicate)
        self.assertEqual(imported.duplicate_finding_id, original.id)
        self.assertIn(AIST_DEDUPE_AUTO_TAG, set(imported.tags.values_list("name", flat=True)))

    def test_exact_reimport_keeps_a_higher_severity_finding_active_for_review(self):
        original_test = self._create_test("Semgrep JSON Report")
        repeated_test = self._create_test("Semgrep JSON Report")
        self._create_finding(
            test=original_test,
            title="SQL injection",
            vuln_id="semgrep-sql-injection",
            unique_id="semgrep-result-77",
            file_path="src/query.py",
            line=19,
            cwe=89,
            severity="Low",
        )
        imported = self._create_finding(
            test=repeated_test,
            title="SQL injection",
            vuln_id="semgrep-sql-injection",
            unique_id="semgrep-result-77",
            file_path="src/query.py",
            line=19,
            cwe=89,
            severity="Critical",
        )

        dedupe_batch_of_findings([imported])
        imported.refresh_from_db()

        self.assertFalse(imported.duplicate)
        self.assertTrue(imported.active)
        self.assertEqual(imported.severity, "Critical")
        self.assertIn(AIST_DEDUPE_CANDIDATE_TAG, set(imported.tags.values_list("name", flat=True)))
        self.assertNotIn(AIST_DEDUPE_AUTO_TAG, set(imported.tags.values_list("name", flat=True)))

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

    def test_coturn_cve_on_two_relay_hosts_is_one_component_defect(self):
        """The two assessed runs expose the same coturn CVE on different relay hosts."""
        binding, _ = self._dast_bindings()
        august_test = self._create_dast_test(binding)
        september_test = self._create_dast_test(binding)
        original = self._create_finding(
            test=august_test,
            title="coturn 4.6.0 pre-authentication OAuth token stack overflow",
            vuln_id="dast-coturn-august",
            cwe=121,
            component_name="coturn",
            component_version="4.6.0",
            service="turn",
            dynamic_finding=True,
        )
        self._add_endpoint(
            original,
            protocol="tcp",
            host="mail.relay.aktt2.cloud.hdw.mx",
            port=3478,
        )
        Vulnerability_Id.objects.create(finding=original, vulnerability_id="CVE-2026-43994")
        imported = self._create_finding(
            test=september_test,
            title="Internet-facing coturn OAuth decode stack overflow",
            vuln_id="dast-coturn-september",
            cwe=121,
            component_name="coturn",
            component_version="4.6.0",
            service="turn",
            dynamic_finding=True,
        )
        self._add_endpoint(
            imported,
            protocol="tcp",
            host="mail.relay-ecs.gui1.cloud.hdw.mx",
            port=3478,
        )
        Vulnerability_Id.objects.create(finding=imported, vulnerability_id="CVE-2026-43994")

        dedupe_batch_of_findings([imported])
        imported.refresh_from_db()

        self.assertTrue(imported.duplicate)
        self.assertEqual(imported.duplicate_finding_id, original.id)
        self.assertIn(AIST_DEDUPE_AUTO_TAG, set(imported.tags.values_list("name", flat=True)))

    def test_same_cve_without_the_same_component_requires_triage(self):
        """A product-wide CVE match alone must never silently merge two dynamic findings."""
        binding, _ = self._dast_bindings()
        first_test = self._create_dast_test(binding)
        second_test = self._create_dast_test(binding)
        original = self._create_finding(
            test=first_test,
            title="Known protocol vulnerability on the legacy relay",
            vuln_id="dast-legacy-relay",
            cwe=121,
            dynamic_finding=True,
        )
        Vulnerability_Id.objects.create(finding=original, vulnerability_id="CVE-2026-43994")
        imported = self._create_finding(
            test=second_test,
            title="Known protocol vulnerability on another network service",
            vuln_id="dast-other-network-service",
            cwe=121,
            dynamic_finding=True,
        )
        Vulnerability_Id.objects.create(finding=imported, vulnerability_id="CVE-2026-43994")

        dedupe_batch_of_findings([imported])
        imported.refresh_from_db()

        self.assertFalse(imported.duplicate)
        self.assertIn(AIST_DEDUPE_CANDIDATE_TAG, set(imported.tags.values_list("name", flat=True)))

    def test_complete_route_identity_auto_links_across_deployment_hosts(self):
        binding, _ = self._dast_bindings()
        first_test = self._create_dast_test(binding)
        second_test = self._create_dast_test(binding)
        original = self._create_finding(
            test=first_test,
            title="Partner lookup exposes another tenant",
            vuln_id="dast-partner-lookup-first",
            cwe=639,
            component_name="partner-api",
            service="https",
            parameter="partner_id",
            dynamic_finding=True,
        )
        self._add_endpoint(
            original,
            protocol="https",
            host="qa-api.example.test",
            port=443,
            path="partners/{partner_id}",
        )
        imported = self._create_finding(
            test=second_test,
            title="Cross-tenant partner record disclosure",
            vuln_id="dast-partner-lookup-second",
            cwe=639,
            component_name="partner-api",
            service="https",
            parameter="partner_id",
            dynamic_finding=True,
        )
        self._add_endpoint(
            imported,
            protocol="https",
            host="prod-api.example.test",
            port=443,
            path="partners/{partner_id}",
        )

        dedupe_batch_of_findings([imported])
        imported.refresh_from_db()

        self.assertTrue(imported.duplicate)
        self.assertEqual(imported.duplicate_finding_id, original.id)
        self.assertIn(AIST_DEDUPE_AUTO_TAG, set(imported.tags.values_list("name", flat=True)))

    def test_exact_route_identity_requires_review_when_auto_merge_would_hide_risk(self):
        binding, _ = self._dast_bindings()
        first_test = self._create_dast_test(binding)
        second_test = self._create_dast_test(binding)
        original = self._create_finding(
            test=first_test,
            title="Partner lookup exposes another tenant",
            vuln_id="dast-partner-risk-first",
            cwe=639,
            component_name="partner-api",
            service="https",
            parameter="partner_id",
            dynamic_finding=True,
            severity="Info",
        )
        self._add_endpoint(
            original,
            protocol="https",
            host="qa-api.example.test",
            port=443,
            path="partners/{partner_id}",
        )
        imported = self._create_finding(
            test=second_test,
            title="Critical cross-tenant partner record disclosure",
            vuln_id="dast-partner-risk-second",
            cwe=639,
            component_name="partner-api",
            service="https",
            parameter="partner_id",
            dynamic_finding=True,
            severity="Critical",
        )
        self._add_endpoint(
            imported,
            protocol="https",
            host="prod-api.example.test",
            port=443,
            path="partners/{partner_id}",
        )

        dedupe_batch_of_findings([imported])
        imported.refresh_from_db()

        self.assertFalse(imported.duplicate)
        self.assertTrue(imported.active)
        self.assertEqual(imported.severity, "Critical")
        self.assertIn(AIST_DEDUPE_CANDIDATE_TAG, set(imported.tags.values_list("name", flat=True)))

    def test_exact_route_identity_does_not_auto_merge_into_a_human_dismissed_root(self):
        binding, _ = self._dast_bindings()
        first_test = self._create_dast_test(binding)
        second_test = self._create_dast_test(binding)
        original = self._create_finding(
            test=first_test,
            title="Partner lookup exposes another tenant",
            vuln_id="dast-partner-dismissed-first",
            cwe=639,
            component_name="partner-api",
            service="https",
            parameter="partner_id",
            dynamic_finding=True,
        )
        Finding.objects.filter(pk=original.pk).update(active=False, false_p=True)
        original.refresh_from_db()
        self._add_endpoint(
            original,
            protocol="https",
            host="qa-api.example.test",
            port=443,
            path="partners/{partner_id}",
        )
        imported = self._create_finding(
            test=second_test,
            title="Cross-tenant partner record disclosure",
            vuln_id="dast-partner-dismissed-second",
            cwe=639,
            component_name="partner-api",
            service="https",
            parameter="partner_id",
            dynamic_finding=True,
        )
        self._add_endpoint(
            imported,
            protocol="https",
            host="prod-api.example.test",
            port=443,
            path="partners/{partner_id}",
        )

        dedupe_batch_of_findings([imported])
        imported.refresh_from_db()

        self.assertFalse(imported.duplicate)
        self.assertTrue(imported.active)
        self.assertIn(AIST_DEDUPE_CANDIDATE_TAG, set(imported.tags.values_list("name", flat=True)))

    def test_route_without_full_producer_identity_stays_a_candidate(self):
        binding, _ = self._dast_bindings()
        first_test = self._create_dast_test(binding)
        second_test = self._create_dast_test(binding)
        original = self._create_finding(
            test=first_test,
            title="Partner lookup exposes another tenant",
            vuln_id="dast-partner-lookup-under-specified-first",
            cwe=639,
            dynamic_finding=True,
        )
        self._add_endpoint(
            original,
            protocol="https",
            host="qa-api.example.test",
            port=443,
            path="partners/{partner_id}",
        )
        imported = self._create_finding(
            test=second_test,
            title="Partner lookup exposes another tenant",
            vuln_id="dast-partner-lookup-under-specified-second",
            cwe=639,
            dynamic_finding=True,
        )
        self._add_endpoint(
            imported,
            protocol="https",
            host="prod-api.example.test",
            port=443,
            path="partners/{partner_id}",
        )

        dedupe_batch_of_findings([imported])
        imported.refresh_from_db()

        self.assertFalse(imported.duplicate)
        self.assertIn(AIST_DEDUPE_CANDIDATE_TAG, set(imported.tags.values_list("name", flat=True)))

    def test_non_unique_route_identity_in_one_test_never_auto_merges_a_later_finding(self):
        binding, _ = self._dast_bindings()
        ambiguous_test = self._create_dast_test(binding)
        later_test = self._create_dast_test(binding)
        for suffix in ("first", "second"):
            finding = self._create_finding(
                test=ambiguous_test,
                title=f"Partner record disclosure {suffix}",
                vuln_id=f"dast-partner-ambiguous-{suffix}",
                cwe=639,
                component_name="partner-api",
                service="https",
                parameter="partner_id",
                dynamic_finding=True,
            )
            self._add_endpoint(
                finding,
                protocol="https",
                host="qa-api.example.test",
                port=443,
                path="partners/{partner_id}",
            )
        imported = self._create_finding(
            test=later_test,
            title="Partner record disclosure on the current run",
            vuln_id="dast-partner-ambiguous-current",
            cwe=639,
            component_name="partner-api",
            service="https",
            parameter="partner_id",
            dynamic_finding=True,
        )
        self._add_endpoint(
            imported,
            protocol="https",
            host="prod-api.example.test",
            port=443,
            path="partners/{partner_id}",
        )

        dedupe_batch_of_findings([imported])
        imported.refresh_from_db()

        self.assertFalse(imported.duplicate)
        self.assertIn(AIST_DEDUPE_CANDIDATE_TAG, set(imported.tags.values_list("name", flat=True)))

    def test_exact_dast_reimport_precedes_ambiguous_route_correlation(self):
        binding, _ = self._dast_bindings()
        first_test = self._create_dast_test(binding)
        repeated_test = self._create_dast_test(binding)
        roots = []
        for suffix in ("first", "second"):
            finding = self._create_finding(
                test=first_test,
                title=f"Partner record disclosure {suffix}",
                vuln_id=f"dast-partner-occurrence-{suffix}",
                unique_id=f"dast-partner-occurrence-{suffix}",
                cwe=639,
                component_name="partner-api",
                service="https",
                parameter="partner_id",
                dynamic_finding=True,
            )
            self._add_endpoint(
                finding,
                protocol="https",
                host="qa-api.example.test",
                port=443,
                path="partners/{partner_id}",
            )
            roots.append(finding)
        imported = self._create_finding(
            test=repeated_test,
            title="Partner record disclosure first",
            vuln_id="dast-partner-occurrence-first",
            unique_id="dast-partner-occurrence-first",
            cwe=639,
            component_name="partner-api",
            service="https",
            parameter="partner_id",
            dynamic_finding=True,
        )
        self._add_endpoint(
            imported,
            protocol="https",
            host="qa-api.example.test",
            port=443,
            path="partners/{partner_id}",
        )

        dedupe_batch_of_findings([imported])
        imported.refresh_from_db()

        self.assertTrue(imported.duplicate)
        self.assertEqual(imported.duplicate_finding_id, roots[0].id)
        self.assertIn(AIST_DEDUPE_AUTO_TAG, set(imported.tags.values_list("name", flat=True)))

    def test_unsafe_exact_dast_match_is_identified_in_pipeline_diagnostics(self):
        binding, _ = self._dast_bindings()
        first_test = self._create_dast_test(binding)
        repeated_test = self._create_dast_test(binding)
        original = self._create_finding(
            test=first_test,
            title="Partner record disclosure",
            vuln_id="dast-partner-occurrence",
            unique_id="dast-partner-occurrence",
            cwe=639,
            component_name="partner-api",
            service="https",
            parameter="partner_id",
            dynamic_finding=True,
            severity="Low",
        )
        self._add_endpoint(
            original,
            protocol="https",
            host="qa-api.example.test",
            port=443,
            path="partners/{partner_id}",
        )
        imported = self._create_finding(
            test=repeated_test,
            title="Critical partner record disclosure",
            vuln_id="dast-partner-occurrence",
            unique_id="dast-partner-occurrence",
            cwe=639,
            component_name="partner-api",
            service="https",
            parameter="partner_id",
            dynamic_finding=True,
            severity="Critical",
        )
        self._add_endpoint(
            imported,
            protocol="https",
            host="prod-api.example.test",
            port=443,
            path="partners/{partner_id}",
        )
        log_path = get_pipeline_log_path(f"dedupe-dast-{repeated_test.id}")
        log_path.write_text("", encoding="utf-8")

        dedupe_batch_of_findings([imported])
        imported.refresh_from_db()
        log_content = log_path.read_text(encoding="utf-8")

        self.assertFalse(imported.duplicate)
        self.assertIn(AIST_DEDUPE_CANDIDATE_TAG, set(imported.tags.values_list("name", flat=True)))
        self.assertIn("source=unique_id_from_tool", log_content)
        self.assertIn("identity=unique_id_from_tool", log_content)
        self.assertIn("reason=canonical_root_would_lower_severity", log_content)

    def test_identical_findings_from_different_dast_bindings_do_not_collide(self):
        """Two targets on one product are separate DAST deduplication namespaces."""
        first_binding, second_binding = self._dast_bindings()
        first_test = self._create_dast_test(first_binding)
        second_test = self._create_dast_test(second_binding)
        original = self._create_finding(
            test=first_test,
            title="Partner API grants access without authorization",
            vuln_id="dast-partner-auth",
            unique_id="dast-partner-auth",
            cwe=862,
            component_name="partner-api",
            service="http",
            dynamic_finding=True,
        )
        self._add_endpoint(
            original,
            protocol="https",
            host="api.example.test",
            port=443,
            path="partners/grants",
        )
        imported = self._create_finding(
            test=second_test,
            title="Partner API grants access without authorization",
            vuln_id="dast-partner-auth",
            unique_id="dast-partner-auth",
            cwe=862,
            component_name="partner-api",
            service="http",
            dynamic_finding=True,
        )
        self._add_endpoint(
            imported,
            protocol="https",
            host="api.example.test",
            port=443,
            path="partners/grants",
        )

        dedupe_batch_of_findings([imported])
        imported.refresh_from_db()

        self.assertFalse(imported.duplicate)
        self.assertNotIn(AIST_DEDUPE_CANDIDATE_TAG, set(imported.tags.values_list("name", flat=True)))
