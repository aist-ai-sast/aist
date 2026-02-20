from __future__ import annotations

from io import StringIO

from django.core.management import call_command
from django.utils import timezone
from dojo.models import Engagement, Finding, Test, Test_Type

from aist.parser_overrides import build_semgrep_humanized_title
from aist.test.test_api import AISTApiBase


class SemgrepTitleMigrationTests(AISTApiBase):
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

    def test_build_semgrep_humanized_title(self):
        title = build_semgrep_humanized_title(
            check_id="generic.secrets.security.detected-private-key.detected-private-key",
            file_path="_internal/arm.rsa.key",
            line=1,
        )
        self.assertEqual(title, "Detected Private Key")

    def test_management_command_updates_only_semgrep_findings(self):
        semgrep_finding = self._create_finding(
            test_type_name="Semgrep JSON Report",
            title="generic.secrets.security.detected-private-key.detected-private-key",
            vuln_id="generic.secrets.security.detected-private-key.detected-private-key",
            file_path="_internal/arm.rsa.key",
            line=1,
        )
        other_finding = self._create_finding(
            test_type_name="Snyk Code Scan",
            title="cpp/BufferOverflow_open/src/dbd_mysql.c",
            vuln_id="cpp/BufferOverflow",
            file_path="open/src/dbd_mysql.c",
            line=358,
        )

        dry_stdout = StringIO()
        call_command("migrate_semgrep_titles", "--dry-run", stdout=dry_stdout)
        semgrep_finding.refresh_from_db()
        self.assertEqual(semgrep_finding.title, "generic.secrets.security.detected-private-key.detected-private-key")

        run_stdout = StringIO()
        call_command("migrate_semgrep_titles", stdout=run_stdout)
        semgrep_finding.refresh_from_db()
        other_finding.refresh_from_db()

        self.assertEqual(semgrep_finding.title, "Detected Private Key")
        self.assertEqual(other_finding.title, "cpp/BufferOverflow_open/src/dbd_mysql.c")
