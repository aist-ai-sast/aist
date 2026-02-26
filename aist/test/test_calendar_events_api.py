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
            script_path="scripts/build.sh",
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

    def test_finding_mitigated_event_is_listed(self):
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
        )
        finding.last_status_update = timezone.now() - timedelta(hours=8)
        finding.save(update_fields=["last_status_update"])
        self.version.findings.add(finding)

        start, end = self._base_range()
        response = self.client.get(
            self._url(),
            data={
                "start": start.isoformat(),
                "end": end.isoformat(),
                "view": "month",
                "event_types": ["finding_mitigated"],
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.data["events"]), 1)
        event = response.data["events"][0]
        self.assertEqual(event["event_type"], "finding_mitigated")
        self.assertTrue(event["is_aggregated"])
        self.assertEqual(event["summary"]["active"], False)

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
