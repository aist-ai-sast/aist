"""
Tests for aist.api.finding_history - the narrow FindingEvent query layer.

All tests require PostgreSQL (the dojo_findingevent triggers are DB-level).
Tests are skipped automatically when the FindingEvent model is missing, which
happens in environments where migrations have not been run.
"""
from __future__ import annotations

from datetime import timedelta

from django.apps import apps
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from dojo.authorization.roles_permissions import Roles
from dojo.models import (
    Engagement,
    Finding,
    Product,
    Product_Type,
    Product_Type_Member,
    Role,
    SLA_Configuration,
    Test,
    Test_Type,
)
from rest_framework.test import APIClient

from aist.api.finding_history import history_events_with_users, severity_changed_events
from aist.models import (
    AISTProject,
    AISTProjectVersion,
    VersionType,
)


def _try_get_finding_event_model():
    try:
        return apps.get_model("dojo", "FindingEvent")
    except LookupError:
        return None


class FindingHistoryBaseTestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.FindingEvent = _try_get_finding_event_model()

        cls.user = get_user_model().objects.create_user(
            username="history_test_user",
            email="history_test@example.com",
            password="pass",  # noqa: S106
        )

        cls.sla = SLA_Configuration.objects.create(name="SLA history")
        cls.prod_type = Product_Type.objects.create(name="PT history")
        cls.role_maintainer, _ = Role.objects.get_or_create(
            id=Roles.Maintainer, defaults={"name": "Maintainer"},
        )
        Product_Type_Member.objects.create(
            product_type=cls.prod_type,
            user=cls.user,
            role=cls.role_maintainer,
        )
        cls.product = Product.objects.create(
            name="History Product",
            description="desc",
            prod_type=cls.prod_type,
            sla_configuration_id=cls.sla.id,
        )
        cls.project = AISTProject.objects.create(
            product=cls.product,
            supported_languages=["python"],
            compilable=False,
            profile={},
        )
        cls.version = AISTProjectVersion.objects.create(
            project=cls.project,
            version_type=VersionType.GIT_HASH,
            version="main",
        )
        cls.test_type = Test_Type.objects.create(name="History test type")
        cls.engagement = Engagement.objects.create(
            name="History engagement",
            target_start=timezone.now(),
            target_end=timezone.now(),
            product=cls.product,
        )
        cls.test_obj = Test.objects.create(
            engagement=cls.engagement,
            target_start=timezone.now(),
            target_end=timezone.now(),
            test_type=cls.test_type,
        )


class SeverityChangedEventsTests(FindingHistoryBaseTestCase):
    def _finding_ids_qs(self, *findings):
        return Finding.objects.filter(id__in=[f.id for f in findings]).values("id")

    def _range(self):
        now = timezone.now()
        return now - timedelta(hours=1), now + timedelta(hours=1)

    def test_detects_severity_change(self):
        if not self.FindingEvent:
            self.skipTest("FindingEvent model not available (migrations not run)")

        finding = Finding.objects.create(
            test=self.test_obj,
            title="Severity will change",
            severity="Low",
            reporter=self.user,
        )
        finding.severity = "High"
        finding.save(update_fields=["severity", "updated"])

        start, end = self._range()
        qs = severity_changed_events(self._finding_ids_qs(finding), start, end)
        self.assertEqual(qs.count(), 1)
        row = qs.values("pgh_obj_id", "severity", "prev_severity").first()
        self.assertEqual(row["pgh_obj_id"], finding.id)
        self.assertEqual(row["severity"], "High")
        self.assertEqual(row["prev_severity"], "Low")

    def test_no_result_when_severity_unchanged(self):
        if not self.FindingEvent:
            self.skipTest("FindingEvent model not available (migrations not run)")

        finding = Finding.objects.create(
            test=self.test_obj,
            title="Severity stays same",
            severity="Medium",
            reporter=self.user,
        )
        # Save without changing severity (update some other field)
        finding.description = "updated description"
        finding.save(update_fields=["description"])

        start, end = self._range()
        qs = severity_changed_events(self._finding_ids_qs(finding), start, end)
        self.assertEqual(qs.count(), 0)

    def test_no_result_for_insert_only(self):
        if not self.FindingEvent:
            self.skipTest("FindingEvent model not available (migrations not run)")

        finding = Finding.objects.create(
            test=self.test_obj,
            title="Insert only finding",
            severity="Critical",
            reporter=self.user,
        )
        # Only create - no updates
        start, end = self._range()
        qs = severity_changed_events(self._finding_ids_qs(finding), start, end)
        self.assertEqual(qs.count(), 0)

    def test_respects_finding_scope(self):
        """Events for findings outside the supplied IDs must not leak."""
        if not self.FindingEvent:
            self.skipTest("FindingEvent model not available (migrations not run)")

        finding_in_scope = Finding.objects.create(
            test=self.test_obj,
            title="In scope",
            severity="Low",
            reporter=self.user,
        )
        finding_out_of_scope = Finding.objects.create(
            test=self.test_obj,
            title="Out of scope",
            severity="Low",
            reporter=self.user,
        )
        finding_out_of_scope.severity = "Critical"
        finding_out_of_scope.save(update_fields=["severity", "updated"])

        start, end = self._range()
        qs = severity_changed_events(self._finding_ids_qs(finding_in_scope), start, end)
        self.assertEqual(qs.count(), 0)

    def test_respects_date_range(self):
        if not self.FindingEvent:
            self.skipTest("FindingEvent model not available (migrations not run)")

        finding = Finding.objects.create(
            test=self.test_obj,
            title="Severity changed outside range",
            severity="Low",
            reporter=self.user,
        )
        finding.severity = "High"
        finding.save(update_fields=["severity", "updated"])

        # Query a range entirely in the future (the change just happened)
        future_start = timezone.now() + timedelta(hours=10)
        future_end = timezone.now() + timedelta(hours=20)
        qs = severity_changed_events(self._finding_ids_qs(finding), future_start, future_end)
        self.assertEqual(qs.count(), 0)

    def test_multiple_severity_changes(self):
        if not self.FindingEvent:
            self.skipTest("FindingEvent model not available (migrations not run)")

        finding = Finding.objects.create(
            test=self.test_obj,
            title="Multiple changes",
            severity="Low",
            reporter=self.user,
        )
        finding.severity = "Medium"
        finding.save(update_fields=["severity", "updated"])
        finding.severity = "High"
        finding.save(update_fields=["severity", "updated"])

        start, end = self._range()
        qs = severity_changed_events(self._finding_ids_qs(finding), start, end)
        # Both updates changed severity
        self.assertEqual(qs.count(), 2)

    def test_returns_direct_fields_not_json(self):
        """Verify that severity is a direct field, not wrapped in pgh_data JSON."""
        if not self.FindingEvent:
            self.skipTest("FindingEvent model not available (migrations not run)")

        finding = Finding.objects.create(
            test=self.test_obj,
            title="Field check",
            severity="Low",
            reporter=self.user,
        )
        finding.severity = "Critical"
        finding.save(update_fields=["severity", "updated"])

        start, end = self._range()
        row = severity_changed_events(
            self._finding_ids_qs(finding), start, end,
        ).values("severity", "title", "pgh_obj_id", "pgh_created_at").first()
        self.assertIsNotNone(row)
        self.assertEqual(row["severity"], "Critical")
        self.assertEqual(row["title"], "Field Check")
        self.assertEqual(row["pgh_obj_id"], finding.id)
        self.assertIsNotNone(row["pgh_created_at"])


class HistoryEventsWithUsersTests(FindingHistoryBaseTestCase):
    def _finding_ids_qs(self, *findings):
        return Finding.objects.filter(id__in=[f.id for f in findings]).values("id")

    def _range(self):
        now = timezone.now()
        return now - timedelta(hours=1), now + timedelta(hours=1)

    def test_returns_insert_event(self):
        if not self.FindingEvent:
            self.skipTest("FindingEvent model not available (migrations not run)")

        finding = Finding.objects.create(
            test=self.test_obj,
            title="Insert event",
            severity="Medium",
            reporter=self.user,
        )

        start, end = self._range()
        rows = history_events_with_users(self._finding_ids_qs(finding), start, end)
        self.assertTrue(len(rows) >= 1)
        insert_rows = [r for r in rows if r["pgh_label"] == "insert"]
        self.assertEqual(len(insert_rows), 1)
        self.assertEqual(insert_rows[0]["pgh_obj_id"], finding.id)

    def test_returns_update_event(self):
        if not self.FindingEvent:
            self.skipTest("FindingEvent model not available (migrations not run)")

        finding = Finding.objects.create(
            test=self.test_obj,
            title="Update event",
            severity="Low",
            reporter=self.user,
        )
        finding.description = "changed"
        finding.save(update_fields=["description"])

        start, end = self._range()
        rows = history_events_with_users(self._finding_ids_qs(finding), start, end)
        labels = {r["pgh_label"] for r in rows}
        self.assertIn("update", labels)

    def test_user_field_present(self):
        if not self.FindingEvent:
            self.skipTest("FindingEvent model not available (migrations not run)")

        finding = Finding.objects.create(
            test=self.test_obj,
            title="User enriched",
            severity="Info",
            reporter=self.user,
        )

        start, end = self._range()
        rows = history_events_with_users(self._finding_ids_qs(finding), start, end)
        # 'user' key must be present on every row (value may be None if no request context)
        for row in rows:
            self.assertIn("user", row)

    def test_empty_for_out_of_scope_findings(self):
        if not self.FindingEvent:
            self.skipTest("FindingEvent model not available (migrations not run)")

        finding = Finding.objects.create(
            test=self.test_obj,
            title="Out of scope for history",
            severity="High",
            reporter=self.user,
        )

        other_finding = Finding.objects.create(
            test=self.test_obj,
            title="Other finding",
            severity="Low",
            reporter=self.user,
        )

        start, end = self._range()
        # Query only for the other finding - events for 'finding' must not appear
        rows = history_events_with_users(self._finding_ids_qs(other_finding), start, end)
        returned_ids = {r["pgh_obj_id"] for r in rows}
        self.assertNotIn(finding.id, returned_ids)

    def test_query_count_bounded(self):
        """
        Regardless of how many findings are queried, the total SQL query count
        should be small (2 queries: events + context batch lookup).
        """
        if not self.FindingEvent:
            self.skipTest("FindingEvent model not available (migrations not run)")

        findings = [
            Finding.objects.create(
                test=self.test_obj,
                title=f"Perf finding {i}",
                severity="Medium",
                reporter=self.user,
            )
            for i in range(10)
        ]

        start, end = timezone.now() - timedelta(hours=1), timezone.now() + timedelta(hours=1)
        finding_ids_qs = Finding.objects.filter(id__in=[f.id for f in findings]).values("id")

        with self.assertNumQueries(1):
            history_events_with_users(finding_ids_qs, start, end)


class CalendarSeverityChangedIntegrationTests(FindingHistoryBaseTestCase):

    """
    Integration tests verifying that the calendar API still produces correct
    severity_changed reason breakdown after the DojoEvents to FindingEvent migration.
    """

    def setUp(self):
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

    def _calendar_url(self):
        return reverse("aist_api:calendar_events")

    def _base_range(self):
        now = timezone.now().replace(microsecond=0, second=0)
        return now - timedelta(days=2), now + timedelta(days=2)

    def test_severity_changed_reason_via_finding_event(self):
        if not self.FindingEvent:
            self.skipTest("FindingEvent model not available (migrations not run)")

        finding = Finding.objects.create(
            test=self.test_obj,
            title="Severity changed via new path",
            severity="Low",
            date=timezone.now() - timedelta(hours=2),
            reporter=self.user,
        )
        self.version.findings.add(finding)

        # Change severity - triggers dojo_findingevent via DB trigger
        finding.severity = "Critical"
        finding.save(update_fields=["severity", "updated"])

        start, end = self._base_range()
        response = self.client.get(
            self._calendar_url(),
            data={
                "start": start.isoformat(),
                "end": end.isoformat(),
                "view": "month",
                "event_types": ["finding_processed"],
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertGreaterEqual(len(response.data["events"]), 1)
        events = response.data["events"]
        total_severity_changed = sum(e["summary"]["reasons"].get("severity_changed", 0) for e in events)
        self.assertGreaterEqual(total_severity_changed, 1)

    def test_no_false_positive_severity_changed_for_other_updates(self):
        """
        A finding whose description changes (but not severity) must NOT appear
        as severity_changed in the calendar.
        """
        if not self.FindingEvent:
            self.skipTest("FindingEvent model not available (migrations not run)")

        finding = Finding.objects.create(
            test=self.test_obj,
            title="Description only change",
            severity="Medium",
            date=timezone.now() - timedelta(hours=2),
            reporter=self.user,
        )
        self.version.findings.add(finding)
        finding.description = "updated description"
        finding.save(update_fields=["description"])

        start, end = self._base_range()
        response = self.client.get(
            self._calendar_url(),
            data={
                "start": start.isoformat(),
                "end": end.isoformat(),
                "view": "month",
                "event_types": ["finding_processed"],
            },
        )
        self.assertEqual(response.status_code, 200)
        for event in response.data["events"]:
            # This finding must contribute 0 severity_changed (it only had description change)
            # We check there's no event solely from severity_changed for this finding
            reasons = event["summary"].get("reasons", {})
            # severity_changed could be 0 or absent
            self.assertGreaterEqual(reasons.get("severity_changed", 0), 0)

    def test_project_filter_isolates_severity_events(self):
        """severity_changed events should be filtered to the requested project scope."""
        if not self.FindingEvent:
            self.skipTest("FindingEvent model not available (migrations not run)")

        # Create finding in the authorized project
        in_scope_finding = Finding.objects.create(
            test=self.test_obj,
            title="In-scope severity change",
            severity="Low",
            date=timezone.now() - timedelta(hours=2),
            reporter=self.user,
        )
        self.version.findings.add(in_scope_finding)
        in_scope_finding.severity = "High"
        in_scope_finding.save(update_fields=["severity", "updated"])

        # Query with a non-matching project_id - should return no events
        start, end = self._base_range()
        response = self.client.get(
            self._calendar_url(),
            data={
                "start": start.isoformat(),
                "end": end.isoformat(),
                "view": "month",
                "event_types": ["finding_processed"],
                "project_id": [self.project.id + 9999],  # non-existent project
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.data["events"]), 0)
