from __future__ import annotations

from io import StringIO

from django.core.management import call_command
from django.utils import timezone
from dojo.models import Engagement, Finding, Test, Test_Type

from aist.test.test_api import AISTApiBase


class BearerTitleMigrationTests(AISTApiBase):
    def _create_finding(self, *, title: str, test_type_name: str = "Bearer CLI") -> Finding:
        engagement = Engagement.objects.create(
            name="Bearer engagement",
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

    def test_management_command_updates_bearer_titles_only(self):
        bearer_finding = self._create_finding(
            title="Unsanitized User Input in Dynamic HTML Insertion (XSS) in src/app/layout.tsx:112",
        )
        other_finding = self._create_finding(
            test_type_name="Semgrep JSON Report",
            title="Detected Private Key",
        )

        dry_stdout = StringIO()
        call_command("migrate_bearer_titles", "--dry-run", stdout=dry_stdout)
        bearer_finding.refresh_from_db()
        self.assertEqual(
            bearer_finding.title,
            "Unsanitized User Input in Dynamic HTML Insertion (XSS) in src/app/layout.tsx:112",
        )

        run_stdout = StringIO()
        call_command("migrate_bearer_titles", stdout=run_stdout)
        bearer_finding.refresh_from_db()
        other_finding.refresh_from_db()

        self.assertEqual(
            bearer_finding.title,
            "Unsanitized User Input in Dynamic HTML Insertion (XSS)",
        )
        self.assertEqual(other_finding.title, "Detected Private Key")
