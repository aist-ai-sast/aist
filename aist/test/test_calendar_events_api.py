from __future__ import annotations

from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase
from django.urls import reverse
from django.utils import timezone
from dojo.authorization.roles_permissions import Roles
from dojo.models import (
    Engagement,
    Finding,
    Notes,
    Product,
    Product_Type,
    Product_Type_Member,
    Role,
    SLA_Configuration,
    Test,
    Test_Type,
)
from rest_framework.test import APIClient

from aist.api.calendar_events import CalendarEventId
from aist.models import (
    AISTPipeline,
    AISTProject,
    AISTProjectLaunchConfig,
    AISTProjectVersion,
    LaunchSchedule,
    VersionType,
)


class CalendarEventIdTests(SimpleTestCase):
    def test_parse_and_build_pipeline_scheduled(self):
        event_id = CalendarEventId.pipeline_scheduled(12, 1730000000).to_string()
        parsed = CalendarEventId.parse(event_id)
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed.event_type, "pipeline_scheduled")
        self.assertEqual(parsed.token, "12:1730000000")

    def test_parse_rejects_unknown_event_type(self):
        self.assertIsNone(CalendarEventId.parse("unknown:1"))


class CalendarEventsApiTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = get_user_model().objects.create_user(
            username="calendar_user",
            email="calendar@example.com",
            password="pass",  # noqa: S106
        )
        self.client.force_authenticate(user=self.user)

        self.sla = SLA_Configuration.objects.create(name="SLA calendar")
        self.prod_type = Product_Type.objects.create(name="PT calendar")
        self.role_maintainer, _ = Role.objects.get_or_create(id=Roles.Maintainer, defaults={"name": "Maintainer"})
        Product_Type_Member.objects.create(
            product_type=self.prod_type,
            user=self.user,
            role=self.role_maintainer,
        )

        self.product = Product.objects.create(
            name="Calendar Product",
            description="desc",
            prod_type=self.prod_type,
            sla_configuration_id=self.sla.id,
        )
        self.project = AISTProject.objects.create(
            product=self.product,
            supported_languages=["python"],
            compilable=False,
            profile={},
        )
        self.version = AISTProjectVersion.objects.create(
            project=self.project,
            version_type=VersionType.GIT_HASH,
            version="main",
        )
        self.pipeline = AISTPipeline.objects.create(id="calendar-pipeline", project=self.project, status="FINISHED")
        self.launch_config = AISTProjectLaunchConfig.objects.create(
            project=self.project,
            name="Nightly",
            params={"log_level": "INFO"},
        )

    def _url(self):
        return reverse("aist_api:calendar_events")

    def _detail_url(self, event_id: str):
        return reverse("aist_api:calendar_event_detail", kwargs={"event_id": event_id})

    def _timeline_url(self):
        return reverse("aist_api:finding_timeline")

    def _base_range(self):
        now = timezone.now().replace(microsecond=0, second=0)
        return now - timedelta(days=2), now + timedelta(days=2)

    def test_requires_authentication(self):
        client = APIClient()
        start, end = self._base_range()
        response = client.get(
            self._url(),
            data={"start": start.isoformat(), "end": end.isoformat(), "view": "month"},
        )
        self.assertIn(response.status_code, (401, 403))

    def test_filters_by_event_type(self):
        start, end = self._base_range()
        response = self.client.get(
            self._url(),
            data={
                "start": start.isoformat(),
                "end": end.isoformat(),
                "view": "month",
                "event_types": ["project_created"],
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertGreaterEqual(len(response.data["events"]), 1)
        self.assertEqual({row["event_type"] for row in response.data["events"]}, {"project_created"})

    def test_finding_created_is_aggregated_with_severity_summary(self):
        test_type = Test_Type.objects.create(name="Calendar finding type")
        engagement = Engagement.objects.create(
            name="Calendar engagement",
            target_start=timezone.now(),
            target_end=timezone.now(),
            product=self.product,
        )
        test = Test.objects.create(
            engagement=engagement,
            target_start=timezone.now(),
            target_end=timezone.now(),
            test_type=test_type,
        )
        created_at = timezone.now() - timedelta(days=1)
        finding_1 = Finding.objects.create(
            test=test,
            title="Calendar finding critical",
            severity="Critical",
            date=created_at,
            reporter=self.user,
        )
        finding_2 = Finding.objects.create(
            test=test,
            title="Calendar finding high",
            severity="High",
            date=created_at,
            reporter=self.user,
        )
        finding_3 = Finding.objects.create(
            test=test,
            title="Calendar finding low",
            severity="Low",
            date=created_at,
            reporter=self.user,
        )
        self.version.findings.add(finding_1, finding_2, finding_3)

        start, end = self._base_range()
        response = self.client.get(
            self._url(),
            data={
                "start": start.isoformat(),
                "end": end.isoformat(),
                "view": "month",
                "event_types": ["finding_created"],
                "grouping": "auto",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.data["events"]), 1)
        event = response.data["events"][0]
        self.assertEqual(event["event_type"], "finding_created")
        self.assertTrue(event["is_aggregated"])
        self.assertEqual(event["count"], 3)
        self.assertEqual(event["summary"]["severity"]["Critical"], 1)
        self.assertEqual(event["summary"]["severity"]["High"], 1)
        self.assertEqual(event["summary"]["severity"]["Low"], 1)

    def test_project_id_filter_limits_results(self):
        start, end = self._base_range()
        response = self.client.get(
            self._url(),
            data={
                "start": start.isoformat(),
                "end": end.isoformat(),
                "view": "month",
                "event_types": ["project_created"],
                "project_id": [self.project.id],
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertGreaterEqual(len(response.data["events"]), 1)
        project_ids = {row["summary"]["project_id"] for row in response.data["events"]}
        self.assertEqual(project_ids, {self.project.id})

    def test_accepts_comma_separated_query_params(self):
        start, end = self._base_range()
        response = self.client.get(
            self._url(),
            data={
                "start": start.isoformat(),
                "end": end.isoformat(),
                "view": "month",
                "event_types": "project_created,pipeline_started",
                "project_id": str(self.project.id),
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertGreaterEqual(len(response.data["events"]), 1)
        for event in response.data["events"]:
            if "project_id" in event["summary"]:
                self.assertEqual(event["summary"]["project_id"], self.project.id)

    def test_pipeline_scheduled_returns_future_events(self):
        now = timezone.localtime(timezone.now()).replace(second=0, microsecond=0)
        future = now + timedelta(days=2)
        LaunchSchedule.objects.create(
            launch_config=self.launch_config,
            cron_expression=f"{future.minute} {future.hour} * * *",
            enabled=True,
        )
        start, end = self._base_range()
        response = self.client.get(
            self._url(),
            data={
                "start": start.isoformat(),
                "end": end.isoformat(),
                "view": "month",
                "event_types": ["pipeline_scheduled"],
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertGreaterEqual(len(response.data["events"]), 1)
        event = next((row for row in response.data["events"] if row["event_type"] == "pipeline_scheduled" and row["is_future"]), None)
        self.assertIsNotNone(event)
        self.assertEqual(event["event_type"], "pipeline_scheduled")
        self.assertIsNone(event["link"])
        self.assertEqual(event["summary"]["project_name"], self.product.name)

    def test_pipeline_scheduled_does_not_duplicate_on_dst_week(self):
        schedule = LaunchSchedule.objects.create(
            launch_config=self.launch_config,
            cron_expression="45 10 * * 1",
            enabled=True,
        )
        response = self.client.get(
            self._url(),
            data={
                "start": "2026-03-29T00:00:00+01:00",
                "end": "2026-04-05T00:00:00+02:00",
                "view": "week",
                "timezone": "Europe/Berlin",
                "event_types": ["pipeline_scheduled"],
            },
        )
        self.assertEqual(response.status_code, 200)
        schedule_events = [
            row
            for row in response.data["events"]
            if row["event_type"] == "pipeline_scheduled"
            and row["summary"].get("schedule_id") == schedule.id
        ]
        self.assertEqual(len(schedule_events), 1)

    def test_finding_processed_event_is_listed(self):
        test_type = Test_Type.objects.create(name="Calendar mitigated type")
        engagement = Engagement.objects.create(
            name="Calendar mitigated engagement",
            target_start=timezone.now(),
            target_end=timezone.now(),
            product=self.product,
        )
        test = Test.objects.create(
            engagement=engagement,
            target_start=timezone.now(),
            target_end=timezone.now(),
            test_type=test_type,
        )
        finding = Finding.objects.create(
            test=test,
            title="Mitigated finding",
            severity="High",
            date=timezone.now() - timedelta(days=1),
            reporter=self.user,
            active=False,
            is_mitigated=True,
        )
        finding.mitigated = timezone.now() - timedelta(hours=8)
        finding.save(update_fields=["mitigated"])
        self.version.findings.add(finding)

        start, end = self._base_range()
        response = self.client.get(
            self._url(),
            data={
                "start": start.isoformat(),
                "end": end.isoformat(),
                "view": "month",
                "event_types": ["finding_processed"],
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.data["events"]), 1)
        event = response.data["events"][0]
        self.assertEqual(event["event_type"], "finding_processed")
        self.assertTrue(event["is_aggregated"])
        self.assertEqual(event["summary"]["reasons"]["mitigated"], 1)

    def test_finding_processed_uses_last_status_update_for_closed_findings(self):
        test_type = Test_Type.objects.create(name="Calendar mitigated strict type")
        engagement = Engagement.objects.create(
            name="Calendar mitigated strict engagement",
            target_start=timezone.now(),
            target_end=timezone.now(),
            product=self.product,
        )
        test = Test.objects.create(
            engagement=engagement,
            target_start=timezone.now(),
            target_end=timezone.now(),
            test_type=test_type,
        )
        outside_range = timezone.now() - timedelta(days=10)
        inside_range = timezone.now() - timedelta(hours=2)
        finding = Finding.objects.create(
            test=test,
            title="Closed not mitigated",
            severity="Medium",
            date=timezone.now() - timedelta(days=1),
            reporter=self.user,
            active=False,
            is_mitigated=False,
            mitigated=outside_range,
            last_status_update=inside_range,
        )
        self.version.findings.add(finding)

        start, end = self._base_range()
        response = self.client.get(
            self._url(),
            data={
                "start": start.isoformat(),
                "end": end.isoformat(),
                "view": "month",
                "event_types": ["finding_processed"],
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.data["events"]), 1)
        self.assertEqual(response.data["events"][0]["summary"]["reasons"]["resolved"], 1)

    def test_finding_processed_includes_severity_changed_reason(self):
        test_type = Test_Type.objects.create(name="Calendar processed severity type")
        engagement = Engagement.objects.create(
            name="Calendar processed severity engagement",
            target_start=timezone.now(),
            target_end=timezone.now(),
            product=self.product,
        )
        test = Test.objects.create(
            engagement=engagement,
            target_start=timezone.now(),
            target_end=timezone.now(),
            test_type=test_type,
        )
        finding = Finding.objects.create(
            test=test,
            title="Severity changed finding",
            severity="Low",
            date=timezone.now() - timedelta(days=1),
            reporter=self.user,
        )
        self.version.findings.add(finding)
        finding.severity = "High"
        finding.save(update_fields=["severity", "updated"])

        start, end = self._base_range()
        response = self.client.get(
            self._url(),
            data={
                "start": start.isoformat(),
                "end": end.isoformat(),
                "view": "month",
                "event_types": ["finding_processed"],
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.data["events"]), 1)
        self.assertEqual(response.data["events"][0]["summary"]["reasons"]["severity_changed"], 1)

    def test_calendar_event_detail_pipeline_started(self):
        start, end = self._base_range()
        response = self.client.get(
            self._url(),
            data={
                "start": start.isoformat(),
                "end": end.isoformat(),
                "view": "month",
                "event_types": ["pipeline_started"],
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.data["events"]), 1)
        event_id = response.data["events"][0]["id"]

        detail = self.client.get(self._detail_url(event_id))
        self.assertEqual(detail.status_code, 200)
        self.assertEqual(detail.data["id"], event_id)
        self.assertEqual(detail.data["event_type"], "pipeline_started")
        self.assertEqual(detail.data["summary"]["pipeline_id"], self.pipeline.id)
        self.assertIn("duration_seconds", detail.data["summary"])

    def test_calendar_event_detail_not_found(self):
        detail = self.client.get(self._detail_url("pipeline_started:not-found"))
        self.assertEqual(detail.status_code, 404)

    def test_finding_timeline_uses_shared_finding_event_stream(self):
        test_type = Test_Type.objects.create(name="Timeline type")
        engagement = Engagement.objects.create(
            name="Timeline engagement",
            target_start=timezone.now(),
            target_end=timezone.now(),
            product=self.product,
        )
        test = Test.objects.create(
            engagement=engagement,
            target_start=timezone.now(),
            target_end=timezone.now(),
            test_type=test_type,
        )
        created_finding = Finding.objects.create(
            test=test,
            title="Timeline created",
            severity="High",
            date=timezone.now() - timedelta(hours=10),
            reporter=self.user,
        )
        mitigated_finding = Finding.objects.create(
            test=test,
            title="Timeline mitigated",
            severity="Medium",
            date=timezone.now() - timedelta(days=2),
            reporter=self.user,
            active=False,
            is_mitigated=True,
            mitigated=timezone.now() - timedelta(hours=1),
        )
        self.version.findings.add(created_finding, mitigated_finding)
        start, end = self._base_range()
        response = self.client.get(
            self._timeline_url(),
            data={
                "start": start.isoformat(),
                "end": end.isoformat(),
                "event_types": ["finding_created", "finding_processed"],
            },
        )
        self.assertEqual(response.status_code, 200)
        event_types = {row["event_type"] for row in response.data["events"]}
        self.assertIn("finding_created", event_types)
        self.assertIn("finding_processed", event_types)
        processed = next(row for row in response.data["events"] if row["event_type"] == "finding_processed")
        self.assertEqual(processed["processed_reason"], "mitigated")

    def test_finding_timeline_filters_by_project(self):
        test_type = Test_Type.objects.create(name="Timeline project filter type")
        engagement = Engagement.objects.create(
            name="Timeline project filter engagement",
            target_start=timezone.now(),
            target_end=timezone.now(),
            product=self.product,
        )
        test = Test.objects.create(
            engagement=engagement,
            target_start=timezone.now(),
            target_end=timezone.now(),
            test_type=test_type,
        )
        finding = Finding.objects.create(
            test=test,
            title="Project timeline finding",
            severity="Low",
            date=timezone.now() - timedelta(hours=3),
            reporter=self.user,
        )
        self.version.findings.add(finding)
        start, end = self._base_range()
        response = self.client.get(
            self._timeline_url(),
            data={
                "start": start.isoformat(),
                "end": end.isoformat(),
                "project_id": [self.project.id],
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertGreaterEqual(len(response.data["events"]), 1)
        for row in response.data["events"]:
            if row["project_ids"]:
                self.assertEqual(set(row["project_ids"]), {self.project.id})

    def test_finding_timeline_includes_owner_and_notes_for_single_finding(self):
        test_type = Test_Type.objects.create(name="Timeline history owner type")
        engagement = Engagement.objects.create(
            name="Timeline history owner engagement",
            target_start=timezone.now(),
            target_end=timezone.now(),
            product=self.product,
        )
        test = Test.objects.create(
            engagement=engagement,
            target_start=timezone.now(),
            target_end=timezone.now(),
            test_type=test_type,
        )
        finding = Finding.objects.create(
            test=test,
            title="Timeline owner finding",
            severity="High",
            date=timezone.now() - timedelta(hours=2),
            reporter=self.user,
        )
        self.version.findings.add(finding)
        note = Notes.objects.create(entry="Owner note", author=self.user, private=False)
        finding.notes.add(note)

        response = self.client.get(
            self._timeline_url(),
            data={
                "start": (timezone.now() - timedelta(days=365)).isoformat(),
                "end": (timezone.now() + timedelta(days=1)).isoformat(),
                "finding_id": finding.id,
                "event_types": ["finding_created", "finding_processed", "finding_note_added"],
            },
        )
        self.assertEqual(response.status_code, 200)
        event_types = {row["event_type"] for row in response.data["events"]}
        self.assertIn("finding_created", event_types)
        self.assertIn("finding_note_added", event_types)
        note_event = next(row for row in response.data["events"] if row["event_type"] == "finding_note_added")
        self.assertEqual(note_event["owner"], self.user.username)
        self.assertEqual(note_event["details"], "Owner note")

    def test_finding_timeline_allows_long_range_for_single_finding(self):
        test_type = Test_Type.objects.create(name="Timeline long history type")
        engagement = Engagement.objects.create(
            name="Timeline long history engagement",
            target_start=timezone.now(),
            target_end=timezone.now(),
            product=self.product,
        )
        test = Test.objects.create(
            engagement=engagement,
            target_start=timezone.now(),
            target_end=timezone.now(),
            test_type=test_type,
        )
        finding = Finding.objects.create(
            test=test,
            title="Timeline long range finding",
            severity="Low",
            date=timezone.now() - timedelta(days=120),
            reporter=self.user,
        )
        self.version.findings.add(finding)
        response = self.client.get(
            self._timeline_url(),
            data={
                "start": (timezone.now() - timedelta(days=180)).isoformat(),
                "end": (timezone.now() + timedelta(days=1)).isoformat(),
                "finding_id": finding.id,
            },
        )
        self.assertEqual(response.status_code, 200)
