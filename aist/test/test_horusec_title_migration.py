from __future__ import annotations

from io import StringIO

from django.core.management import call_command
from django.utils import timezone
from dojo.models import Engagement, Finding, Test, Test_Type

from aist.parser_overrides import normalize_horusec_title
from aist.test.test_api import AISTApiBase


class HorusecTitleMigrationTests(AISTApiBase):
    def _create_finding(
        self,
        *,
        title: str,
        test_type_name: str = "Horusec Scan",
        description: str = "",
        cwe: int | None = None,
    ) -> Finding:
        engagement = Engagement.objects.create(
            name="Horusec engagement",
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
            description=description,
            cwe=cwe,
        )

    def test_normalize_horusec_title_removes_standard_prefix(self):
        raw = "(1/1) * Possible Vulnerability Detected: SQL Injection in request param"
        self.assertEqual(normalize_horusec_title(raw), "SQL Injection in request param")

    def test_management_command_updates_horusec_titles_only(self):
        horusec_finding = self._create_finding(
            title="(12/33) * Possible Vulnerability Detected: Hardcoded secret",
            description="Horusec details mention CWE-489 here",
            cwe=0,
        )
        other_finding = self._create_finding(
            test_type_name="Semgrep JSON Report",
            title="Detected Private Key",
            description="Contains CWE-338 but must not be changed by Horusec migration",
            cwe=0,
        )

        dry_stdout = StringIO()
        call_command("migrate_horusec_titles", "--dry-run", stdout=dry_stdout)
        horusec_finding.refresh_from_db()
        other_finding.refresh_from_db()
        self.assertEqual(horusec_finding.title, "(12/33) * Possible Vulnerability Detected: Hardcoded Secret")
        self.assertEqual(horusec_finding.cwe, 0)
        self.assertEqual(other_finding.cwe, 0)

        run_stdout = StringIO()
        call_command("migrate_horusec_titles", stdout=run_stdout)
        horusec_finding.refresh_from_db()
        other_finding.refresh_from_db()

        self.assertEqual(horusec_finding.title, "Hardcoded Secret")
        self.assertEqual(horusec_finding.cwe, 489)
        self.assertEqual(other_finding.title, "Detected Private Key")
        self.assertEqual(other_finding.cwe, 0)

    def test_management_command_keeps_existing_valid_horusec_cwe(self):
        horusec_finding = self._create_finding(
            title="(1/1) * Possible Vulnerability Detected: Weak random",
            description="Horusec details with CWE-338",
            cwe=295,
        )

        call_command("migrate_horusec_titles")
        horusec_finding.refresh_from_db()

        self.assertEqual(horusec_finding.title, "Weak Random")
        self.assertEqual(horusec_finding.cwe, 295)

    def test_management_command_uses_title_as_fallback_source_for_cwe(self):
        horusec_finding = self._create_finding(
            title="(1/1) * Possible Vulnerability Detected: Debug enabled CWE-489",
            description="",
            cwe=0,
        )

        call_command("migrate_horusec_titles")
        horusec_finding.refresh_from_db()

        self.assertEqual(horusec_finding.title, "Debug Enabled CWE-489")
        self.assertEqual(horusec_finding.cwe, 489)
