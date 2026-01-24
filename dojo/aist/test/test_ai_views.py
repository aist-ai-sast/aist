from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import patch

from django.urls import reverse
from django.utils import timezone

from dojo.aist.models import AISTAIResponse, AISTPipeline, AISTStatus
from dojo.aist.test.test_api import AISTApiBase
from dojo.models import Engagement, Finding, Test, Test_Type


class AISTAIViewsTests(AISTApiBase):
    def _json(self, resp):
        return json.loads(resp.content.decode("utf-8") or "{}")

    def setUp(self):
        super().setUp()
        self.client.force_login(self.user)
        self.engagement = Engagement.objects.create(
            name="Engage",
            target_start=timezone.now(),
            target_end=timezone.now(),
            product=self.product,
        )
        self.semgrep = Test_Type.objects.create(name="Semgrep")
        self.trivy = Test_Type.objects.create(name="Trivy")
        self.test_semgrep = Test.objects.create(
            engagement=self.engagement,
            target_start=timezone.now(),
            target_end=timezone.now(),
            test_type=self.semgrep,
        )
        self.test_trivy = Test.objects.create(
            engagement=self.engagement,
            target_start=timezone.now(),
            target_end=timezone.now(),
            test_type=self.trivy,
        )

    def test_product_analyzers_json_distinct(self):
        Finding.objects.create(
            test=self.test_semgrep,
            title="A",
            severity="High",
            date=timezone.now(),
            reporter=self.user,
        )
        Finding.objects.create(
            test=self.test_semgrep,
            title="B",
            severity="Low",
            date=timezone.now(),
            reporter=self.user,
        )
        Finding.objects.create(
            test=self.test_trivy,
            title="C",
            severity="Low",
            date=timezone.now(),
            reporter=self.user,
        )

        url = reverse("dojo_aist:product_analyzers_json", kwargs={"product_id": self.product.id})
        resp = self.client.get(url)

        self.assertEqual(resp.status_code, 200)
        data = self._json(resp)
        keys = {a["key"] for a in data["analyzers"]}
        self.assertIn("semgrep", keys)
        self.assertIn("trivy", keys)

    def test_search_findings_json_filters(self):
        f1 = Finding.objects.create(
            test=self.test_semgrep,
            title="SQL Injection",
            severity="High",
            cwe=89,
            date=timezone.now(),
            reporter=self.user,
        )
        Finding.objects.create(
            test=self.test_trivy,
            title="Info",
            severity="Low",
            cwe=1,
            date=timezone.now(),
            reporter=self.user,
        )

        url = reverse("dojo_aist:search_findings_json")
        resp = self.client.get(
            url,
            data={
                "product": self.product.id,
                "analyzers": "semgrep",
                "cwe": "89",
                "query": "SQL",
            },
        )

        self.assertEqual(resp.status_code, 200)
        results = self._json(resp)["results"]
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["id"], f1.id)

    @patch("dojo.aist.views.ai.install_pipeline_logging")
    @patch("dojo.aist.views.ai.push_request_to_ai")
    def test_send_request_to_ai_happy_path(self, mock_push, mock_log):
        mock_log.return_value = SimpleNamespace(error=lambda *_a, **_k: None)
        pipeline = AISTPipeline.objects.create(
            id="pipe-1",
            project=self.project,
            project_version=self.pv,
            status=AISTStatus.WAITING_CONFIRMATION_TO_PUSH_TO_AI,
        )
        f1 = Finding.objects.create(
            test=self.test_semgrep,
            title="SQL",
            severity="High",
            date=timezone.now(),
            reporter=self.user,
        )

        url = reverse("dojo_aist:send_request_to_ai", kwargs={"pipeline_id": pipeline.id})
        resp = self.client.post(
            url,
            data=json.dumps({"finding_ids": [f1.id], "filters": {"limit": 1}}),
            content_type="application/json",
        )

        self.assertEqual(resp.status_code, 200)
        pipeline.refresh_from_db()
        self.assertEqual(pipeline.status, AISTStatus.PUSH_TO_AI)
        mock_push.delay.assert_called_once_with(pipeline.id, [f1.id], {"limit": 1})

    def test_send_request_to_ai_rejects_invalid_ids(self):
        pipeline = AISTPipeline.objects.create(
            id="pipe-2",
            project=self.project,
            project_version=self.pv,
            status=AISTStatus.WAITING_CONFIRMATION_TO_PUSH_TO_AI,
        )
        url = reverse("dojo_aist:send_request_to_ai", kwargs={"pipeline_id": pipeline.id})
        resp = self.client.post(
            url,
            data=json.dumps({"finding_ids": ["bad"]}),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 400)

    def test_ai_filter_reference(self):
        url = reverse("dojo_aist:ai_filter_reference")
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)
        data = self._json(resp)
        self.assertTrue(data["ok"])
        self.assertIn("EQUALS", data["comparisons"])
        self.assertTrue(data["keywords"])
        self.assertTrue(data["fields"])

    def test_ai_filter_help_page(self):
        url = reverse("dojo_aist:ai_filter_help")
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)
        body = resp.content.decode("utf-8")
        self.assertIn("AI Filter Help", body)
        self.assertIn("limit", body)

    def test_export_ai_results_requires_ai_response(self):
        pipeline = AISTPipeline.objects.create(
            id="pipe-export-1",
            project=self.project,
            project_version=self.pv,
            status=AISTStatus.FINISHED,
        )
        url = reverse("dojo_aist:export_ai_results", kwargs={"pipeline_id": pipeline.id})
        resp = self.client.post(url, data={"format": "csv"})
        self.assertEqual(resp.status_code, 400)

    def test_export_ai_results_csv_filters_false_positives(self):
        pipeline = AISTPipeline.objects.create(
            id="pipe-export-2",
            project=self.project,
            project_version=self.pv,
            status=AISTStatus.FINISHED,
        )
        payload = {
            "results": [
                {
                    "title": "Real Finding",
                    "originalFinding": {"file": "a.py", "line": 10, "cwe": 79, "snippet": "x = 1"},
                    "reasoning": "ok",
                    "falsePositive": False,
                    "impactScore": 2,
                },
                {
                    "title": "False Positive",
                    "originalFinding": {"file": "b.py", "line": 20, "cwe": 80, "snippet": "y = 2"},
                    "reasoning": "no",
                    "falsePositive": True,
                    "impactScore": 5,
                },
            ],
        }
        AISTAIResponse.objects.create(pipeline=pipeline, payload=payload)

        url = reverse("dojo_aist:export_ai_results", kwargs={"pipeline_id": pipeline.id})
        resp = self.client.post(
            url,
            data={"format": "csv", "columns": ["title", "file"]},
        )

        self.assertEqual(resp.status_code, 200)
        content = resp.content.decode("utf-8").strip().splitlines()
        self.assertEqual(content[0], "Title,File")
        self.assertEqual(content[1], "Real Finding,a.py")
        self.assertEqual(len(content), 2)

    def test_export_ai_results_csv_includes_false_positive_column(self):
        pipeline = AISTPipeline.objects.create(
            id="pipe-export-3",
            project=self.project,
            project_version=self.pv,
            status=AISTStatus.FINISHED,
        )
        payload = {
            "results": [
                {
                    "title": "Low Impact",
                    "originalFinding": {"file": "a.py", "line": 10},
                    "reasoning": "ok",
                    "falsePositive": False,
                    "impactScore": 1,
                },
                {
                    "title": "High Impact FP",
                    "originalFinding": {"file": "b.py", "line": 20},
                    "reasoning": "no",
                    "falsePositive": True,
                    "impactScore": 9,
                },
            ],
        }
        AISTAIResponse.objects.create(pipeline=pipeline, payload=payload)

        url = reverse("dojo_aist:export_ai_results", kwargs={"pipeline_id": pipeline.id})
        resp = self.client.post(
            url,
            data={
                "format": "csv",
                "columns": ["title"],
                "ignore_false_positives": "0",
            },
        )

        self.assertEqual(resp.status_code, 200)
        content = resp.content.decode("utf-8").strip().splitlines()
        self.assertEqual(content[0], "Title,False positive")
        self.assertEqual(content[1], "High Impact FP,True")
        self.assertEqual(content[2], "Low Impact,False")
