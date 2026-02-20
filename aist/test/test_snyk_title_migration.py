from __future__ import annotations

from io import StringIO

from django.core.management import call_command
from django.utils import timezone
from dojo.models import Engagement, Finding, Test, Test_Type

from aist.test.test_api import AISTApiBase
from aist.utils.snyk_title_migration import build_snyk_humanized_title


class SnykTitleMigrationTests(AISTApiBase):
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

    def test_build_humanized_title_from_legacy_fields(self):
        finding = self._create_finding(
            test_type_name="Snyk Code Scan",
            title="cpp/BufferOverflow_open/src/dbd_mysql.c",
            vuln_id="cpp/BufferOverflow",
            file_path="open/src/dbd_mysql.c",
            line=358,
        )

        title = build_snyk_humanized_title(finding)

        self.assertEqual(title, "Buffer Overflow")

    def test_build_humanized_title_handles_or_and_test_tail(self):
        finding_or = self._create_finding(
            test_type_name="Snyk Code Scan",
            title="javascript/OR_web/router.js",
            vuln_id="javascript/OR",
            file_path="web/router.js",
            line=12,
        )
        finding_test_tail = self._create_finding(
            test_type_name="Snyk Code Scan",
            title="python/NoHardcodedPasswords/test_security/config.py",
            vuln_id="python/NoHardcodedPasswords/test",
            file_path="security/config.py",
            line=7,
        )

        self.assertEqual(build_snyk_humanized_title(finding_or), "Open Redirect Vulnerability")
        self.assertEqual(build_snyk_humanized_title(finding_test_tail), "No Hardcoded Passwords")

    def test_management_command_updates_only_snyk_findings(self):
        snyk_finding = self._create_finding(
            test_type_name="SnykCode Scan (Snyk Code Scan)",
            title="cpp/BufferOverflow_open/src/dbd_mysql.c",
            vuln_id="cpp/BufferOverflow",
            file_path="open/src/dbd_mysql.c",
            line=358,
        )
        other_finding = self._create_finding(
            test_type_name="Semgrep JSON Report",
            title="Semgrep legacy title",
            vuln_id="",
            file_path="open/src/rule.py",
            line=12,
        )

        dry_stdout = StringIO()
        call_command("migrate_snyk_code_titles", "--dry-run", stdout=dry_stdout)
        snyk_finding.refresh_from_db()
        self.assertEqual(snyk_finding.title, "cpp/BufferOverflow_open/src/dbd_mysql.c")

        run_stdout = StringIO()
        call_command("migrate_snyk_code_titles", stdout=run_stdout)
        snyk_finding.refresh_from_db()
        other_finding.refresh_from_db()

        self.assertEqual(snyk_finding.title, "Buffer Overflow")
        self.assertEqual(other_finding.title, "Semgrep legacy title")
