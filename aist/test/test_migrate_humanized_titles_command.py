from __future__ import annotations

from io import StringIO

from django.core.management import call_command
from django.utils import timezone
from dojo.models import Engagement, Finding, Test, Test_Type

from aist.test.test_api import AISTApiBase


class MigrateHumanizedTitlesCommandTests(AISTApiBase):
    def _create_finding(self, *, test_type_name: str, title: str, vuln_id: str, file_path: str, line: int) -> Finding:
        engagement = Engagement.objects.create(
            name=f"{test_type_name} engagement",
            target_start=timezone.now(),
            target_end=timezone.now(),
            product=self.product,
        )
        test_type = Test_Type.objects.create(name=test_type_name)
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
            file_path=file_path,
            line=line,
        )

    def test_command_updates_snyk_and_semgrep(self):
        snyk_finding = self._create_finding(
            test_type_name="SnykCode Scan (Snyk Code Scan)",
            title="cpp/BufferOverflow_open/src/dbd_mysql.c",
            vuln_id="cpp/BufferOverflow",
            file_path="open/src/dbd_mysql.c",
            line=358,
        )
        semgrep_finding = self._create_finding(
            test_type_name="Semgrep JSON Report",
            title="generic.secrets.security.detected-private-key.detected-private-key",
            vuln_id="generic.secrets.security.detected-private-key.detected-private-key",
            file_path="_internal/arm.rsa.key",
            line=1,
        )

        stdout = StringIO()
        call_command("migrate_humanized_titles", stdout=stdout)
        output = stdout.getvalue()

        snyk_finding.refresh_from_db()
        semgrep_finding.refresh_from_db()

        self.assertEqual(snyk_finding.title, "Buffer Overflow")
        self.assertEqual(semgrep_finding.title, "Detected Private Key")
        self.assertIn("migrate_humanized_titles[snyk]", output)
        self.assertIn("migrate_humanized_titles[semgrep]", output)

    def test_command_scan_type_semgrep_does_not_touch_snyk_titles(self):
        snyk_finding = self._create_finding(
            test_type_name="SnykCode Scan (Snyk Code Scan)",
            title="cpp/BufferOverflow_open/src/dbd_mysql.c",
            vuln_id="cpp/BufferOverflow",
            file_path="open/src/dbd_mysql.c",
            line=358,
        )
        semgrep_finding = self._create_finding(
            test_type_name="Semgrep JSON Report",
            title="generic.secrets.security.detected-private-key.detected-private-key",
            vuln_id="generic.secrets.security.detected-private-key.detected-private-key",
            file_path="_internal/arm.rsa.key",
            line=1,
        )

        call_command("migrate_humanized_titles", "--scan-type", "semgrep")
        snyk_finding.refresh_from_db()
        semgrep_finding.refresh_from_db()

        # Snyk must remain untouched when only Semgrep migration is requested.
        self.assertEqual(snyk_finding.title, "cpp/BufferOverflow_open/src/dbd_mysql.c")
        self.assertEqual(semgrep_finding.title, "Detected Private Key")

    def test_command_scan_type_horusec_updates_only_horusec(self):
        horusec_finding = self._create_finding(
            test_type_name="Horusec Scan",
            title="(1/1) * Possible Vulnerability Detected: Hardcoded secret",
            vuln_id="",
            file_path="irrelevant.py",
            line=1,
        )
        semgrep_finding = self._create_finding(
            test_type_name="Semgrep JSON Report",
            title="generic.secrets.security.detected-private-key.detected-private-key",
            vuln_id="generic.secrets.security.detected-private-key.detected-private-key",
            file_path="_internal/arm.rsa.key",
            line=1,
        )

        call_command("migrate_humanized_titles", "--scan-type", "horusec")
        horusec_finding.refresh_from_db()
        semgrep_finding.refresh_from_db()

        self.assertEqual(horusec_finding.title, "Hardcoded secret")
        self.assertEqual(semgrep_finding.title, "generic.secrets.security.detected-private-key.detected-private-key")

    def test_command_scan_type_bearer_updates_only_bearer(self):
        bearer_finding = self._create_finding(
            test_type_name="Bearer CLI",
            title="Unsanitized User Input in Dynamic HTML Insertion (XSS) in src/app/layout.tsx:112",
            vuln_id="",
            file_path="src/app/layout.tsx",
            line=112,
        )
        snyk_finding = self._create_finding(
            test_type_name="SnykCode Scan (Snyk Code Scan)",
            title="cpp/BufferOverflow_open/src/dbd_mysql.c",
            vuln_id="cpp/BufferOverflow",
            file_path="open/src/dbd_mysql.c",
            line=358,
        )

        call_command("migrate_humanized_titles", "--scan-type", "bearer")
        bearer_finding.refresh_from_db()
        snyk_finding.refresh_from_db()

        self.assertEqual(
            bearer_finding.title,
            "Unsanitized User Input in Dynamic HTML Insertion (XSS)",
        )
        self.assertEqual(snyk_finding.title, "cpp/BufferOverflow_open/src/dbd_mysql.c")
