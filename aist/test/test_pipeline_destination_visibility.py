from __future__ import annotations

from datetime import timedelta

from django.urls import reverse
from django.utils import timezone
from dojo.authorization.roles_permissions import Roles
from dojo.models import Engagement, Finding, Product, Product_Type_Member, Role, Test, Test_Type

from aist.api.calendar_events import CalendarEventId
from aist.models import AISTPipeline, AISTProject, AISTStatus, Organization, PipelineExecutionType, ProjectAccessDenial
from aist.test.test_api import AISTApiBase


class PipelineDestinationVisibilityTests(AISTApiBase):

    """A readable launch pipeline must not reveal its denied destination's results."""

    def setUp(self):
        super().setUp()
        self.client.force_login(self.user)
        Organization.objects.create(name="Multi-project reports", product_type=self.prod_type)
        reader, _ = Role.objects.get_or_create(id=Roles.Reader, defaults={"name": "Reader"})
        Product_Type_Member.objects.filter(user=self.user).update(role=reader)
        destination_product = Product.objects.create(
            name="Hidden report destination", description="Second report section",
            prod_type=self.prod_type, sla_configuration=self.sla,
        )
        self.destination = AISTProject.objects.create(product=destination_product)
        self.denial = ProjectAccessDenial.objects.create(project=self.destination, user=self.user)
        self.pipeline = AISTPipeline.objects.create(
            id="multi-project-report", project=self.project, project_version=self.pv,
            execution_type=PipelineExecutionType.MANUAL_IMPORT, status=AISTStatus.FINISHED,
        )
        test_type = Test_Type.objects.create(name="DAST visibility")
        self.tests = []
        for product, count in ((self.product, 1), (destination_product, 2)):
            engagement = Engagement.objects.create(
                name=f"DAST {product.name}", product=product,
                target_start=timezone.now(), target_end=timezone.now(),
            )
            test = Test.objects.create(
                engagement=engagement, test_type=test_type, title=f"DAST {product.name}",
                target_start=timezone.now(), target_end=timezone.now(),
            )
            self.pipeline.tests.add(test)
            self.tests.append(test)
            for index in range(count):
                Finding.objects.create(
                    test=test, title=f"{product.name} finding {index}", severity="High",
                    date=timezone.now(), reporter=self.user,
                )

    def test_deduplication_progress_filters_destination_tests_and_totals(self):
        url = reverse("aist_api:pipeline_deduplication_progress", kwargs={"pipeline_id": self.pipeline.pk})
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual([row["test_id"] for row in response.data["tests"]], [self.tests[0].pk])
        self.assertEqual(response.data["overall"]["total_findings"], 1)
        self.assertNotContains(response, self.tests[1].title)

        self.denial.delete()
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual({row["test_id"] for row in response.data["tests"]}, {test.pk for test in self.tests})
        self.assertEqual(response.data["overall"]["total_findings"], 3)

    def test_calendar_list_and_detail_count_only_visible_destinations(self):
        now = timezone.now()
        params = {
            "start": (now - timedelta(days=1)).isoformat(),
            "end": (now + timedelta(days=1)).isoformat(),
            "view": "month", "event_types": ["pipeline_started"], "project_id": [self.project.pk],
        }
        event_id = CalendarEventId.pipeline_started(self.pipeline.pk).to_string()
        detail_url = reverse("aist_api:calendar_event_detail", kwargs={"event_id": event_id})
        for expected in (1, 3):
            with self.subTest(visible_findings=expected):
                response = self.client.get(reverse("aist_api:calendar_events"), data=params)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(len(response.data["events"]), 1)
                self.assertEqual(response.data["events"][0]["summary"]["findings"], expected)
                response = self.client.get(detail_url, data={"project_id": [self.project.pk]})
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.data["summary"]["findings"], expected)
            if expected == 1:
                self.denial.delete()

    def test_pipeline_summary_counts_only_visible_destinations(self):
        url = reverse("client_pipeline_summary")
        for expected in (1, 3):
            with self.subTest(visible_findings=expected):
                response = self.client.get(url, data={"project_id": self.project.pk})
                self.assertEqual(response.status_code, 200)
                rows = response.json()["results"]
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0]["findings"], expected)
            if expected == 1:
                self.denial.delete()

    def test_legacy_progress_preserves_superuser_visibility(self):
        self.user.is_superuser = True
        self.user.save(update_fields=["is_superuser"])
        url = reverse("aist:deduplication_progress", kwargs={"pipeline_id": self.pipeline.pk})
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual({row["test_id"] for row in response.json()["tests"]}, {test.pk for test in self.tests})
        self.assertEqual(response.json()["overall"]["total_findings"], 3)
