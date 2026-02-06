from __future__ import annotations

from django.urls import reverse
from django.utils import timezone
from dojo.models import Engagement, Finding, Test, Test_Type

from aist.models import AISTPipeline, AISTStatus
from aist.test.test_api import AISTApiBase


class AISTPipelineDetailFindingsTests(AISTApiBase):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.user)
        self.engagement = Engagement.objects.create(
            name="Engage",
            target_start=timezone.now(),
            target_end=timezone.now(),
            product=self.product,
        )
        self.test_type = Test_Type.objects.create(name="Semgrep")
        self.test = Test.objects.create(
            engagement=self.engagement,
            target_start=timezone.now(),
            target_end=timezone.now(),
            test_type=self.test_type,
        )
        self.pipeline = AISTPipeline.objects.create(
            id="pipe-views-1",
            project=self.project,
            project_version=self.pv,
            status=AISTStatus.FINISHED,
        )
        self.pipeline.tests.add(self.test)

    def test_pipeline_detail_lists_findings(self):
        finding = Finding.objects.create(
            test=self.test,
            title="SQL Injection",
            severity="High",
            date=timezone.now(),
            reporter=self.user,
        )
        self.pipeline.launch_data = {
            "ai": {
                "filter_snapshot": {
                    "limit": 25,
                    "severity": [{"comparison": "EQUALS", "value": "HIGH"}],
                },
            },
        }
        self.pipeline.save(update_fields=["launch_data"])

        url = reverse("aist:pipeline_detail", kwargs={"pipeline_id": self.pipeline.id})
        resp = self.client.get(url)

        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Findings")
        self.assertContains(resp, finding.title)
        self.assertContains(resp, "Apply AI filter")
        self.assertContains(resp, "View filter JSON")

    def test_pipeline_detail_applies_ai_filter_when_requested(self):
        high = Finding.objects.create(
            test=self.test,
            title="Critical SQL",
            severity="High",
            date=timezone.now(),
            reporter=self.user,
        )
        low = Finding.objects.create(
            test=self.test,
            title="Info issue",
            severity="Low",
            date=timezone.now(),
            reporter=self.user,
        )
        self.pipeline.launch_data = {
            "ai": {
                "filter_snapshot": {
                    "limit": 50,
                    "severity": [{"comparison": "EQUALS", "value": "HIGH"}],
                },
            },
        }
        self.pipeline.save(update_fields=["launch_data"])

        url = reverse("aist:pipeline_detail", kwargs={"pipeline_id": self.pipeline.id})
        resp = self.client.get(url, data={"apply_ai_filter": "1"})

        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, high.title)
        self.assertNotContains(resp, low.title)
        self.assertContains(resp, "Clear AI filter")

    def test_pipeline_detail_sorts_by_title(self):
        first = Finding.objects.create(
            test=self.test,
            title="Alpha",
            severity="Low",
            date=timezone.now(),
            reporter=self.user,
        )
        second = Finding.objects.create(
            test=self.test,
            title="Zulu",
            severity="Low",
            date=timezone.now(),
            reporter=self.user,
        )

        url = reverse("aist:pipeline_detail", kwargs={"pipeline_id": self.pipeline.id})
        resp = self.client.get(url, data={"findings_sort": "title", "findings_dir": "asc"})

        self.assertEqual(resp.status_code, 200)
        body = resp.content.decode("utf-8")
        self.assertLess(body.find(first.title), body.find(second.title))

    def test_pipeline_detail_denies_other_product(self):
        other = AISTPipeline.objects.create(
            id="pipe-views-other",
            project=self.other_project,
            project_version=self.other_pv,
            status=AISTStatus.FINISHED,
        )
        url = reverse("aist:pipeline_detail", kwargs={"pipeline_id": other.id})
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 404)
