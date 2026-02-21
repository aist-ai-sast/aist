from __future__ import annotations

from io import StringIO

from django.core.management import call_command
from django.utils import timezone
from dojo.models import Engagement, Finding, Test, Test_Type

from aist.parser_overrides import normalize_horusec_title
from aist.test.test_api import AISTApiBase


class HorusecTitleMigrationTests(AISTApiBase):
    def _create_finding(self, *, title: str, test_type_name: str = "Horusec Scan") -> Finding:
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
        )

    def test_normalize_horusec_title_removes_standard_prefix(self):
        raw = "(1/1) * Possible Vulnerability Detected: SQL Injection in request param"
        self.assertEqual(normalize_horusec_title(raw), "SQL Injection in request param")

    def test_management_command_updates_horusec_titles_only(self):
        horusec_finding = self._create_finding(
            title="(12/33) * Possible Vulnerability Detected: Hardcoded secret",
        )
        other_finding = self._create_finding(
            test_type_name="Semgrep JSON Report",
            title="Detected Private Key",
        )

        dry_stdout = StringIO()
        call_command("migrate_horusec_titles", "--dry-run", stdout=dry_stdout)
        horusec_finding.refresh_from_db()
        self.assertEqual(horusec_finding.title, "(12/33) * Possible Vulnerability Detected: Hardcoded Secret")

        run_stdout = StringIO()
        call_command("migrate_horusec_titles", stdout=run_stdout)
        horusec_finding.refresh_from_db()
        other_finding.refresh_from_db()

        self.assertEqual(horusec_finding.title, "Hardcoded Secret")
        self.assertEqual(other_finding.title, "Detected Private Key")
