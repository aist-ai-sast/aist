# aist/test/test_api.py
from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.db import IntegrityError
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from dojo.authorization.roles_permissions import Roles
from dojo.models import (
    Engagement,
    Finding,
    Notes,
    Product,
    Product_Member,
    Product_Type,
    Product_Type_Member,
    Role,
    SLA_Configuration,
    Test,
    Test_Type,
)
from drf_spectacular.generators import SchemaGenerator
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient

from aist.models import (
    AISTAIFindingResponse,
    AISTAIResponse,
    AISTPipeline,
    AISTProject,
    AISTProjectLaunchConfig,
    AISTProjectVersion,
    AISTStatus,
    Organization,
    VersionType,
)
from aist.utils.ai_response import sync_ai_finding_responses
from aist.utils.secrets import MASKED_VALUE


class AISTApiBase(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = get_user_model().objects.create_user(
            username="tester",
            email="tester@example.com",
            password="pass",  # noqa: S106
        )
        self.client.force_authenticate(user=self.user)

        self.sla = SLA_Configuration.objects.create(name="SLA default")
        self.prod_type = Product_Type.objects.create(name="PT")
        self.role_maintainer, _ = Role.objects.get_or_create(
            id=Roles.Maintainer,
            defaults={"name": "Maintainer"},
        )
        self.product = Product.objects.create(
            name="Test Product",
            description="desc",
            prod_type=self.prod_type,
            sla_configuration_id=self.sla.id,
        )
        Product_Type_Member.objects.create(
            product_type=self.prod_type,
            user=self.user,
            role=self.role_maintainer,
        )

        self.project = AISTProject.objects.create(
            product=self.product,
            supported_languages=["python"],
            script_path="scripts/build.sh",
            compilable=False,
            profile={},
        )

        self.pv = AISTProjectVersion.objects.create(
            project=self.project,
            version_type=VersionType.GIT_HASH,
            version="main",
        )

        self.other_prod_type = Product_Type.objects.create(name="PT Other")
        self.other_product = Product.objects.create(
            name="Other Product",
            description="desc",
            prod_type=self.other_prod_type,
            sla_configuration_id=self.sla.id,
        )
        self.other_project = AISTProject.objects.create(
            product=self.other_product,
            supported_languages=["python"],
            script_path="scripts/build.sh",
            compilable=False,
            profile={},
        )
        self.other_pv = AISTProjectVersion.objects.create(
            project=self.other_project,
            version_type=VersionType.GIT_HASH,
            version="other",
        )


class PipelineStartAPITests(AISTApiBase):
    def _url(self):
        # api_urls.py: path("pipelines/start/", ...)
        return reverse("aist_api:pipeline_start")

    @patch("aist.api.pipelines.run_sast_pipeline")
    @patch("aist.api.pipelines.PipelineArguments.normalize_params")
    def test_start_pipeline_happy_path_calls_celery_with_params(
            self, mock_normalize, mock_run_task,
    ):
        mock_normalize.return_value = {
            "project_id": self.project.id,
            "project_version": {"id": self.pv.id},
            "log_level": "INFO",
        }
        mock_run_task.delay.return_value = SimpleNamespace(id="celery-123")

        resp = self.client.post(
            self._url(),
            data={
                "project_version_id": self.pv.id,
                "ai_filter": {
                    "limit": 50,
                    "severity": [{"comparison": "EQUALS", "value": "HIGH"}],
                },
            },
            format="json",
        )

        self.assertEqual(resp.status_code, 201)

        pipeline_id = resp.data["id"]

        mock_run_task.delay.assert_called_once_with(
            pipeline_id,
            mock_normalize.return_value,
        )

    def test_start_pipeline_returns_400_if_filter_required_and_missing(self):
        resp = self.client.post(
            self._url(),
            data={"project_version_id": self.pv.id},
            format="json",
        )

        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.data, {"ai_filter": "ai_filter is required for AUTO_DEFAULT"})


class PipelineCallbackAPITests(AISTApiBase):
    def test_pipeline_callback_accepts_token_on_api_url(self):
        superuser = get_user_model().objects.create_superuser(
            username="callback_admin",
            email="callback_admin@example.com",
            password="pass",  # noqa: S106
        )
        token = Token.objects.create(user=superuser)
        pipeline = AISTPipeline.objects.create(
            id="pipe-callback-api",
            project=self.project,
            status=AISTStatus.WAITING_RESULT_FROM_AI,
        )

        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f"Token {token.key}")
        resp = client.post(
            reverse("aist_api:pipeline_callback", kwargs={"pipeline_id": pipeline.id}),
            data={"results": {"true_positives": []}},
            format="json",
        )

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data, {"ok": True})
        self.assertTrue(AISTAIResponse.objects.filter(pipeline=pipeline).exists())

    def test_pipeline_callback_creates_ai_finding_responses_for_valid_findings_only(self):
        superuser = get_user_model().objects.create_superuser(
            username="callback_admin_ai_finding",
            email="callback_admin_ai_finding@example.com",
            password="pass",  # noqa: S106
        )
        token = Token.objects.create(user=superuser)
        pipeline = AISTPipeline.objects.create(
            id="pipe-callback-ai-findings",
            project=self.project,
            status=AISTStatus.WAITING_RESULT_FROM_AI,
        )

        engagement = Engagement.objects.create(
            name="Engage Callback",
            target_start=timezone.now(),
            target_end=timezone.now(),
            product=self.product,
        )
        test_type = Test_Type.objects.create(name="Semgrep callback")
        test = Test.objects.create(
            engagement=engagement,
            target_start=timezone.now(),
            target_end=timezone.now(),
            test_type=test_type,
        )
        finding = Finding.objects.create(
            test=test,
            title="Callback finding",
            severity="High",
            date=timezone.now(),
            reporter=self.user,
        )
        missing_finding_id = finding.id + 1000

        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f"Token {token.key}")
        resp = client.post(
            reverse("aist_api:pipeline_callback", kwargs={"pipeline_id": pipeline.id}),
            data={
                "job_id": "internal-job-id",
                "results": {
                    "true_positives": [
                        {
                            "title": "Valid finding",
                            "reasoning": "valid",
                            "originalFinding": {"id": finding.id},
                        },
                        {
                            "title": "Missing finding",
                            "reasoning": "missing",
                            "originalFinding": {"id": missing_finding_id},
                        },
                    ],
                },
            },
            format="json",
        )

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(AISTAIFindingResponse.objects.filter(pipeline=pipeline).count(), 1)
        row = AISTAIFindingResponse.objects.get(pipeline=pipeline, finding_id=finding.id)
        self.assertEqual(row.verdict, AISTAIFindingResponse.Verdict.TRUE_POSITIVE)
        self.assertEqual(row.summary, "valid")

    def test_pipeline_callback_closes_false_positive_finding_with_ai_note(self):
        superuser = get_user_model().objects.create_superuser(
            username="callback_admin_ai_fp",
            email="callback_admin_ai_fp@example.com",
            password="pass",  # noqa: S106
        )
        token = Token.objects.create(user=superuser)
        pipeline = AISTPipeline.objects.create(
            id="pipe-callback-ai-fp",
            project=self.project,
            status=AISTStatus.WAITING_RESULT_FROM_AI,
        )

        engagement = Engagement.objects.create(
            name="Engage Callback FP",
            target_start=timezone.now(),
            target_end=timezone.now(),
            product=self.product,
        )
        test_type = Test_Type.objects.create(name="Semgrep callback fp")
        test = Test.objects.create(
            engagement=engagement,
            target_start=timezone.now(),
            target_end=timezone.now(),
            test_type=test_type,
        )
        finding = Finding.objects.create(
            test=test,
            title="Callback FP finding",
            severity="High",
            date=timezone.now(),
            reporter=self.user,
            active=True,
            false_p=False,
            is_mitigated=False,
        )

        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f"Token {token.key}")
        resp = client.post(
            reverse("aist_api:pipeline_callback", kwargs={"pipeline_id": pipeline.id}),
            data={
                "results": {
                    "false_positives": [
                        {
                            "title": "FP finding",
                            "reasoning": "should close",
                            "originalFinding": {"id": finding.id},
                        },
                    ],
                },
            },
            format="json",
        )

        self.assertEqual(resp.status_code, 200)
        finding.refresh_from_db()
        self.assertFalse(finding.active)
        self.assertTrue(finding.false_p)
        self.assertTrue(finding.is_mitigated)
        self.assertTrue(finding.notes.filter(entry__contains="AI mitigated").exists())
        row = AISTAIFindingResponse.objects.get(pipeline=pipeline, finding_id=finding.id)
        self.assertEqual(row.verdict, AISTAIFindingResponse.Verdict.FALSE_POSITIVE)

    def test_old_ui_callback_url_is_not_available(self):
        superuser = get_user_model().objects.create_superuser(
            username="callback_admin_old_url",
            email="callback_admin_old@example.com",
            password="pass",  # noqa: S106
        )
        token = Token.objects.create(user=superuser)
        pipeline = AISTPipeline.objects.create(
            id="pipe-callback-old-url",
            project=self.project,
            status=AISTStatus.WAITING_RESULT_FROM_AI,
        )

        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f"Token {token.key}")
        resp = client.post(
            f"/aist-admin/aist/pipelines/{pipeline.id}/callback/",
            data={"results": {"true_positives": []}},
            format="json",
        )

        self.assertEqual(resp.status_code, 404)


class AISTAuthorizationTests(AISTApiBase):
    def test_project_list_does_not_grant_access_via_product_member_only(self):
        isolated_type = Product_Type.objects.create(name="Isolated PT")
        isolated_product = Product.objects.create(
            name="Isolated Product",
            description="desc",
            prod_type=isolated_type,
            sla_configuration_id=self.sla.id,
        )
        isolated_project = AISTProject.objects.create(
            product=isolated_product,
            supported_languages=["python"],
            script_path="scripts/build.sh",
            compilable=False,
            profile={},
        )

        limited_user = get_user_model().objects.create_user(
            username="product_member_only_user",
            email="product-member-only@example.com",
            password="pass",  # noqa: S106
        )
        Product_Member.objects.create(
            product=isolated_product,
            user=limited_user,
            role=self.role_maintainer,
        )

        self.client.force_authenticate(user=limited_user)
        resp = self.client.get(reverse("aist_api:project_list"))
        self.assertEqual(resp.status_code, 200)
        rows = resp.data.get("results", resp.data)
        ids = {row["id"] for row in rows}
        self.assertNotIn(isolated_project.id, ids)

    def test_project_list_filters_to_authorized_products(self):
        resp = self.client.get(reverse("aist_api:project_list"))
        self.assertEqual(resp.status_code, 200)
        rows = resp.data.get("results", resp.data)
        ids = {row["id"] for row in rows}
        self.assertIn(self.project.id, ids)
        self.assertNotIn(self.other_project.id, ids)
        row = next((item for item in rows if item["id"] == self.project.id), None)
        self.assertIsNotNone(row)
        self.assertEqual(row["product_id"], self.product.id)

    def test_project_detail_denies_other_product(self):
        resp = self.client.get(
            reverse("aist_api:project_detail", kwargs={"project_id": self.other_project.id}),
        )
        self.assertEqual(resp.status_code, 404)

    def test_pipeline_list_filters_to_authorized_products(self):
        own = AISTPipeline.objects.create(
            id="pipe-own",
            project=self.project,
            status=AISTStatus.FINISHED,
        )
        AISTPipeline.objects.create(
            id="pipe-other",
            project=self.other_project,
            status=AISTStatus.FINISHED,
        )

        resp = self.client.get(reverse("aist_api:pipelines"))
        self.assertEqual(resp.status_code, 200)
        results = resp.data.get("results", [])
        ids = {row["id"] for row in results}
        self.assertIn(own.id, ids)
        self.assertNotIn("pipe-other", ids)

    def test_pipeline_detail_denies_other_product(self):
        AISTPipeline.objects.create(
            id="pipe-other",
            project=self.other_project,
            status=AISTStatus.FINISHED,
        )
        resp = self.client.get(reverse("aist_api:pipeline_status", kwargs={"pipeline_id": "pipe-other"}))
        self.assertEqual(resp.status_code, 404)

    def test_pipeline_status_stream_denies_other_product(self):
        AISTPipeline.objects.create(
            id="pipe-other-stream",
            project=self.other_project,
            status=AISTStatus.FINISHED,
        )
        resp = self.client.get(
            reverse("aist_api:pipeline_status_stream", kwargs={"pipeline_id": "pipe-other-stream"}),
        )
        self.assertEqual(resp.status_code, 404)

    def test_pipeline_logs_full_denies_other_product(self):
        AISTPipeline.objects.create(
            id="pipe-other-logs",
            project=self.other_project,
            status=AISTStatus.FINISHED,
        )
        resp = self.client.get(
            reverse("aist_api:pipeline_logs_full", kwargs={"pipeline_id": "pipe-other-logs"}),
        )
        self.assertEqual(resp.status_code, 404)


class AISTFindingAuthorizationTests(AISTApiBase):
    def setUp(self):
        super().setUp()
        self.test_type = Test_Type.objects.create(name="Semgrep auth findings")

        own_engagement = Engagement.objects.create(
            name="Own engagement",
            target_start=timezone.now(),
            target_end=timezone.now(),
            product=self.product,
        )
        own_test = Test.objects.create(
            engagement=own_engagement,
            target_start=timezone.now(),
            target_end=timezone.now(),
            test_type=self.test_type,
        )
        self.own_finding = Finding.objects.create(
            test=own_test,
            title="Own finding",
            severity="High",
            date=timezone.now(),
            reporter=self.user,
        )

        other_engagement = Engagement.objects.create(
            name="Other engagement",
            target_start=timezone.now(),
            target_end=timezone.now(),
            product=self.other_product,
        )
        other_test = Test.objects.create(
            engagement=other_engagement,
            target_start=timezone.now(),
            target_end=timezone.now(),
            test_type=self.test_type,
        )
        self.other_finding = Finding.objects.create(
            test=other_test,
            title="Other finding",
            severity="Low",
            date=timezone.now(),
            reporter=self.user,
        )

    def test_finding_list_hides_other_product_findings(self):
        resp = self.client.get(reverse("aist_api:finding_list"))
        self.assertEqual(resp.status_code, 200)
        ids = {row["id"] for row in resp.data.get("results", [])}
        self.assertIn(self.own_finding.id, ids)
        self.assertNotIn(self.other_finding.id, ids)

    def test_finding_notes_denies_other_product_finding(self):
        resp = self.client.get(
            reverse("aist_api:finding_notes", kwargs={"finding_id": self.other_finding.id}),
        )
        self.assertEqual(resp.status_code, 404)

    def test_finding_list_does_not_grant_access_via_product_member_only(self):
        isolated_type = Product_Type.objects.create(name="Isolated Finding PT")
        isolated_product = Product.objects.create(
            name="Isolated Finding Product",
            description="desc",
            prod_type=isolated_type,
            sla_configuration_id=self.sla.id,
        )
        isolated_project = AISTProject.objects.create(
            product=isolated_product,
            supported_languages=["python"],
            script_path="scripts/build.sh",
            compilable=False,
            profile={},
        )
        isolated_engagement = Engagement.objects.create(
            name="Isolated engagement",
            target_start=timezone.now(),
            target_end=timezone.now(),
            product=isolated_product,
        )
        isolated_test = Test.objects.create(
            engagement=isolated_engagement,
            target_start=timezone.now(),
            target_end=timezone.now(),
            test_type=self.test_type,
        )
        isolated_finding = Finding.objects.create(
            test=isolated_test,
            title="Isolated finding",
            severity="High",
            date=timezone.now(),
            reporter=self.user,
        )
        pv = AISTProjectVersion.objects.create(
            project=isolated_project,
            version_type=VersionType.GIT_HASH,
            version="isolated",
        )
        pv.findings.add(isolated_finding)

        limited_user = get_user_model().objects.create_user(
            username="product_member_only_finding_user",
            email="product-member-only-finding@example.com",
            password="pass",  # noqa: S106
        )
        Product_Member.objects.create(
            product=isolated_product,
            user=limited_user,
            role=self.role_maintainer,
        )

        self.client.force_authenticate(user=limited_user)
        resp = self.client.get(reverse("aist_api:finding_list"))
        self.assertEqual(resp.status_code, 200)
        ids = {row["id"] for row in resp.data.get("results", [])}
        self.assertNotIn(isolated_finding.id, ids)

    def test_finding_list_filters_by_created_gte(self):
        self.own_finding.date = timezone.now() - timedelta(days=5)
        self.own_finding.save(update_fields=["date"])
        newer = Finding.objects.create(
            test=self.own_finding.test,
            title="Newer finding",
            severity="Medium",
            date=timezone.now() - timedelta(days=1),
            reporter=self.user,
        )

        cutoff = (timezone.now() - timedelta(days=2)).isoformat()
        resp = self.client.get(reverse("aist_api:finding_list"), data={"created_gte": cutoff})
        self.assertEqual(resp.status_code, 200)
        ids = {row["id"] for row in resp.data.get("results", [])}
        self.assertIn(newer.id, ids)
        self.assertNotIn(self.own_finding.id, ids)

    def test_finding_list_filters_by_created_lte(self):
        self.own_finding.date = timezone.now() - timedelta(days=5)
        self.own_finding.save(update_fields=["date"])
        newer = Finding.objects.create(
            test=self.own_finding.test,
            title="Newest finding",
            severity="Medium",
            date=timezone.now() - timedelta(hours=6),
            reporter=self.user,
        )

        cutoff = (timezone.now() - timedelta(days=2)).isoformat()
        resp = self.client.get(reverse("aist_api:finding_list"), data={"created_lte": cutoff})
        self.assertEqual(resp.status_code, 200)
        ids = {row["id"] for row in resp.data.get("results", [])}
        self.assertIn(self.own_finding.id, ids)
        self.assertNotIn(newer.id, ids)


class AIFindingResponseAPITests(AISTApiBase):
    def test_returns_sanitized_ai_finding_responses_without_job_id(self):
        pipeline = AISTPipeline.objects.create(
            id="pipe-ai-finding-api",
            project=self.project,
            status=AISTStatus.FINISHED,
        )
        engagement = Engagement.objects.create(
            name="Engage AI API",
            target_start=timezone.now(),
            target_end=timezone.now(),
            product=self.product,
        )
        test_type = Test_Type.objects.create(name="Semgrep ai api")
        test = Test.objects.create(
            engagement=engagement,
            target_start=timezone.now(),
            target_end=timezone.now(),
            test_type=test_type,
        )
        finding = Finding.objects.create(
            test=test,
            title="Finding for AI API",
            severity="Medium",
            date=timezone.now(),
            reporter=self.user,
        )
        ai_response = AISTAIResponse.objects.create(
            pipeline=pipeline,
            payload={"job_id": "internal-only", "results": {}},
        )
        AISTAIFindingResponse.objects.create(
            pipeline=pipeline,
            source_response=ai_response,
            finding=finding,
            verdict=AISTAIFindingResponse.Verdict.FALSE_POSITIVE,
            title="AI title",
            summary="AI reasoning",
            references=["https://owasp.org/www-community/vulnerabilities/Insecure_Randomness"],
            epss_score=0.12,
        )

        resp = self.client.get(
            reverse("aist_api:ai_finding_responses"),
            data={"project_id": self.project.id, "finding_ids": str(finding.id)},
        )

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(resp.data), 1)
        item = resp.data[0]
        self.assertEqual(item["finding_id"], finding.id)
        self.assertEqual(item["pipeline_id"], pipeline.id)
        self.assertEqual(item["verdict"], "false_positive")
        self.assertEqual(item["reasoning"], "AI reasoning")
        self.assertNotIn("job_id", item)

    def test_keeps_reference_urls_as_received(self):
        pipeline = AISTPipeline.objects.create(
            id="pipe-ai-reference-normalize",
            project=self.project,
            status=AISTStatus.FINISHED,
        )
        engagement = Engagement.objects.create(
            name="Engage AI refs",
            target_start=timezone.now(),
            target_end=timezone.now(),
            product=self.product,
        )
        test_type = Test_Type.objects.create(name="Semgrep ai refs")
        test = Test.objects.create(
            engagement=engagement,
            target_start=timezone.now(),
            target_end=timezone.now(),
            test_type=test_type,
        )
        finding = Finding.objects.create(
            test=test,
            title="Finding for refs",
            severity="Low",
            date=timezone.now(),
            reporter=self.user,
        )
        ai_response = AISTAIResponse.objects.create(pipeline=pipeline, payload={"results": {}})
        AISTAIFindingResponse.objects.create(
            pipeline=pipeline,
            source_response=ai_response,
            finding=finding,
            verdict=AISTAIFindingResponse.Verdict.UNCERTAIN,
            title="refs",
            summary="refs",
            references=["example.com/path", "https://already.valid"],
        )

        resp = self.client.get(
            reverse("aist_api:ai_finding_responses"),
            data={"project_id": self.project.id, "finding_ids": str(finding.id)},
        )

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data[0]["references"], ["example.com/path", "https://already.valid"])

    def test_sync_ai_finding_responses_ignores_entries_without_original_finding_id(self):
        pipeline = AISTPipeline.objects.create(
            id="pipe-ai-sync",
            project=self.project,
            status=AISTStatus.FINISHED,
        )
        engagement = Engagement.objects.create(
            name="Engage AI Sync",
            target_start=timezone.now(),
            target_end=timezone.now(),
            product=self.product,
        )
        test_type = Test_Type.objects.create(name="Semgrep ai sync")
        test = Test.objects.create(
            engagement=engagement,
            target_start=timezone.now(),
            target_end=timezone.now(),
            test_type=test_type,
        )
        finding = Finding.objects.create(
            test=test,
            title="Finding for sync",
            severity="Low",
            date=timezone.now(),
            reporter=self.user,
        )
        ai_response = AISTAIResponse.objects.create(
            pipeline=pipeline,
            payload={
                "results": {
                    "true_positives": [
                        {"title": "No finding id", "originalFinding": {}},
                        {"title": "Valid", "reasoning": "ok", "originalFinding": {"id": finding.id}},
                    ],
                },
            },
        )

        stats = sync_ai_finding_responses(pipeline=pipeline, ai_response=ai_response)

        self.assertEqual(stats.saved, 1)
        self.assertEqual(stats.dropped, 1)
        self.assertTrue(AISTAIFindingResponse.objects.filter(pipeline=pipeline, finding=finding).exists())


class AISTFindingAIFilterTests(AISTApiBase):
    def setUp(self):
        super().setUp()
        self.engagement = Engagement.objects.create(
            name="Engage AI filter",
            target_start=timezone.now(),
            target_end=timezone.now(),
            product=self.product,
        )
        self.test_type = Test_Type.objects.create(name="Semgrep ai filter")
        self.test = Test.objects.create(
            engagement=self.engagement,
            target_start=timezone.now(),
            target_end=timezone.now(),
            test_type=self.test_type,
        )
        self.finding_with_ai = Finding.objects.create(
            test=self.test,
            title="Finding with AI",
            severity="High",
            date=timezone.now(),
            reporter=self.user,
        )
        self.finding_without_ai = Finding.objects.create(
            test=self.test,
            title="Finding without AI",
            severity="Low",
            date=timezone.now(),
            reporter=self.user,
        )
        self.pipeline = AISTPipeline.objects.create(
            id="pipe-ai-filter",
            project=self.project,
            status=AISTStatus.FINISHED,
        )
        self.pipeline.tests.add(self.test)
        ai_response = AISTAIResponse.objects.create(pipeline=self.pipeline, payload={"results": {}})
        AISTAIFindingResponse.objects.create(
            pipeline=self.pipeline,
            source_response=ai_response,
            finding=self.finding_with_ai,
            verdict=AISTAIFindingResponse.Verdict.TRUE_POSITIVE,
            title="AI title",
            summary="AI reasoning",
        )

    def test_finding_list_filters_has_ai_response(self):
        resp = self.client.get(
            reverse("aist_api:finding_list"),
            data={"test__engagement__product": self.product.id, "ai_status": "has_ai"},
        )

        self.assertEqual(resp.status_code, 200)
        ids = [row["id"] for row in resp.data["results"]]
        self.assertIn(self.finding_with_ai.id, ids)
        self.assertNotIn(self.finding_without_ai.id, ids)
        self.assertEqual(resp.data["count"], 1)

    def test_finding_list_filters_no_ai_response(self):
        resp = self.client.get(
            reverse("aist_api:finding_list"),
            data={"test__engagement__product": self.product.id, "ai_status": "no_ai"},
        )

        self.assertEqual(resp.status_code, 200)
        ids = [row["id"] for row in resp.data["results"]]
        self.assertIn(self.finding_without_ai.id, ids)
        self.assertNotIn(self.finding_with_ai.id, ids)
        self.assertEqual(resp.data["count"], 1)

    def test_finding_list_rejects_invalid_ai_response_filter(self):
        resp = self.client.get(
            reverse("aist_api:finding_list"),
            data={"test__engagement__product": self.product.id, "ai_status": "invalid"},
        )
        self.assertEqual(resp.status_code, 400)

    def test_finding_list_filters_by_ai_tp_status(self):
        resp = self.client.get(
            reverse("aist_api:finding_list"),
            data={"test__engagement__product": self.product.id, "ai_status": "ai_tp"},
        )

        self.assertEqual(resp.status_code, 200)
        ids = [row["id"] for row in resp.data["results"]]
        self.assertEqual(ids, [self.finding_with_ai.id])

    def test_finding_list_filters_by_ai_fp_status(self):
        resp = self.client.get(
            reverse("aist_api:finding_list"),
            data={"test__engagement__product": self.product.id, "ai_status": "ai_fp"},
        )

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data["count"], 0)


class AISTFindingTagsTests(AISTApiBase):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.user)
        self.extra_product = Product.objects.create(
            name="Extra Product",
            description="desc",
            prod_type=self.prod_type,
            sla_configuration_id=self.sla.id,
        )
        self.extra_project = AISTProject.objects.create(
            product=self.extra_product,
            supported_languages=["python"],
            script_path="scripts/build.sh",
            compilable=False,
            profile={},
        )
        self.engagement = Engagement.objects.create(
            name="Engage",
            target_start=timezone.now(),
            target_end=timezone.now(),
            product=self.product,
        )
        self.other_engagement = Engagement.objects.create(
            name="Engage Other",
            target_start=timezone.now(),
            target_end=timezone.now(),
            product=self.extra_product,
        )
        self.test_type = Test_Type.objects.create(name="Semgrep")
        self.test = Test.objects.create(
            engagement=self.engagement,
            target_start=timezone.now(),
            target_end=timezone.now(),
            test_type=self.test_type,
        )
        self.other_test = Test.objects.create(
            engagement=self.other_engagement,
            target_start=timezone.now(),
            target_end=timezone.now(),
            test_type=self.test_type,
        )
        self.finding = Finding.objects.create(
            test=self.test,
            title="Finding A",
            severity="High",
            date=timezone.now(),
            reporter=self.user,
        )
        self.finding.tags = "security,domxss"
        self.finding.save()

        self.other_finding = Finding.objects.create(
            test=self.other_test,
            title="Finding B",
            severity="Low",
            date=timezone.now(),
            reporter=self.user,
        )
        self.other_finding.tags = "other"
        self.other_finding.save()

    def test_finding_tags_returns_global_tags(self):
        url = reverse("aist_api:finding_tags")
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)
        tags = resp.data.get("tags", [])
        self.assertIn("security", tags)
        self.assertIn("domxss", tags)
        self.assertIn("other", tags)

    def test_finding_tags_filters_by_project(self):
        url = reverse("aist_api:finding_tags")
        resp = self.client.get(url, data={"project_id": self.project.id})
        self.assertEqual(resp.status_code, 200)
        tags = resp.data.get("tags", [])
        self.assertIn("security", tags)
        self.assertIn("domxss", tags)
        self.assertNotIn("other", tags)

    def test_finding_list_tags_or(self):
        url = reverse("aist_api:finding_list")
        resp = self.client.get(url, data={"tags": "security,other"})
        self.assertEqual(resp.status_code, 200)
        results = resp.data.get("results", [])
        ids = {row["id"] for row in results}
        self.assertIn(self.finding.id, ids)
        self.assertIn(self.other_finding.id, ids)

    def test_finding_list_filters_by_pipeline(self):
        other_test = Test.objects.create(
            engagement=self.engagement,
            target_start=timezone.now(),
            target_end=timezone.now(),
            test_type=self.test_type,
        )
        other_finding = Finding.objects.create(
            test=other_test,
            title="Finding C",
            severity="Medium",
            date=timezone.now(),
            reporter=self.user,
        )
        pipeline = AISTPipeline.objects.create(
            id="pipe-filter",
            project=self.project,
            status=AISTStatus.FINISHED,
        )
        pipeline.tests.add(self.test)

        url = reverse("aist_api:finding_list")
        resp = self.client.get(url, data={"pipeline_id": pipeline.id})
        self.assertEqual(resp.status_code, 200)
        results = resp.data.get("results", [])
        ids = {row["id"] for row in results}
        self.assertIn(self.finding.id, ids)
        self.assertNotIn(other_finding.id, ids)

    def test_finding_list_filters_by_project_version_and_file(self):
        pv_hash = AISTProjectVersion.objects.create(
            project=self.project,
            version_type=VersionType.GIT_HASH,
            version="aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        )
        self.finding.file_path = "src/app/main.py"
        self.finding.save(update_fields=["file_path"])
        self.other_finding.file_path = "src/lib/helper.py"
        self.other_finding.save(update_fields=["file_path"])
        pv_hash.findings.add(self.finding)

        url = reverse("aist_api:finding_list")
        resp = self.client.get(url, data={"project_version": pv_hash.version, "file": "main.py"})
        self.assertEqual(resp.status_code, 200)

        results = resp.data.get("results", [])
        ids = {row["id"] for row in results}
        self.assertIn(self.finding.id, ids)
        self.assertNotIn(self.other_finding.id, ids)

    def test_finding_list_filters_by_project_id(self):
        pv_main = AISTProjectVersion.objects.create(
            project=self.project,
            version_type=VersionType.GIT_HASH,
            version="1111111111111111111111111111111111111111",
        )
        pv_other = AISTProjectVersion.objects.create(
            project=self.extra_project,
            version_type=VersionType.GIT_HASH,
            version="2222222222222222222222222222222222222222",
        )
        pv_main.findings.add(self.finding)
        pv_other.findings.add(self.other_finding)

        url = reverse("aist_api:finding_list")
        resp = self.client.get(url, data={"project_id": self.project.id})
        self.assertEqual(resp.status_code, 200)

        results = resp.data.get("results", [])
        ids = {row["id"] for row in results}
        self.assertIn(self.finding.id, ids)
        self.assertNotIn(self.other_finding.id, ids)

    def test_finding_list_includes_project_version_and_created(self):
        pv_hash = AISTProjectVersion.objects.create(
            project=self.project,
            version_type=VersionType.GIT_HASH,
            version="bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
        )
        pv_hash.findings.add(self.finding)

        url = reverse("aist_api:finding_list")
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)

        rows = {row["id"]: row for row in resp.data.get("results", [])}
        row = rows[self.finding.id]
        self.assertEqual(row.get("project_id"), self.project.id)
        self.assertEqual(row.get("project_version"), pv_hash.version)
        self.assertEqual(row.get("project_version_type"), VersionType.GIT_HASH)
        self.assertIn("created", row)

    def test_finding_list_filters_by_multiple_severities(self):
        self.other_finding.severity = "Critical"
        self.other_finding.save(update_fields=["severity"])
        medium_finding = Finding.objects.create(
            test=self.test,
            title="Finding M",
            severity="Medium",
            date=timezone.now(),
            reporter=self.user,
        )

        url = reverse("aist_api:finding_list")
        resp = self.client.get(url, data={"severity": "High,Critical"})
        self.assertEqual(resp.status_code, 200)
        ids = {row["id"] for row in resp.data.get("results", [])}
        self.assertIn(self.finding.id, ids)
        self.assertIn(self.other_finding.id, ids)
        self.assertNotIn(medium_finding.id, ids)

    def test_finding_list_ordering_alias_accepts_single_value(self):
        url = reverse("aist_api:finding_list")
        resp = self.client.get(url, data={"ordering": "numerical_severity"})
        self.assertEqual(resp.status_code, 200)
        self.assertIn("results", resp.data)

    def test_finding_list_sql_injection_payloads_do_not_bypass_filters(self):
        pv_hash = AISTProjectVersion.objects.create(
            project=self.project,
            version_type=VersionType.GIT_HASH,
            version="dddddddddddddddddddddddddddddddddddddddd",
        )
        pv_hash.findings.add(self.finding)
        self.finding.file_path = "src/app/main.py"
        self.finding.save(update_fields=["file_path"])

        url = reverse("aist_api:finding_list")

        project_version_injection = self.client.get(
            url,
            data={"project_version": "' OR 1=1 --"},
        )
        self.assertEqual(project_version_injection.status_code, 200)
        self.assertEqual(project_version_injection.data.get("results", []), [])

        file_injection = self.client.get(
            url,
            data={"file": "' OR 1=1 --"},
        )
        self.assertEqual(file_injection.status_code, 200)
        self.assertEqual(file_injection.data.get("results", []), [])

        severity_injection = self.client.get(
            url,
            data={"severity": "High,' OR 1=1 --"},
        )
        self.assertEqual(severity_injection.status_code, 200)
        ids = {row["id"] for row in severity_injection.data.get("results", [])}
        self.assertIn(self.finding.id, ids)
        self.assertNotIn(self.other_finding.id, ids)


class AISTProductSummaryTests(AISTApiBase):
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
        Finding.objects.create(
            test=self.test,
            title="Critical Finding",
            severity="Critical",
            date=timezone.now(),
            reporter=self.user,
            active=True,
        )
        Finding.objects.create(
            test=self.test,
            title="Low Finding",
            severity="Low",
            date=timezone.now(),
            reporter=self.user,
            active=False,
        )
        AISTPipeline.objects.create(
            id="pipe-summary",
            project=self.project,
            status=AISTStatus.FINISHED,
        )

    def test_product_summary_counts(self):
        resp = self.client.get(reverse("client_product_summary"))
        self.assertEqual(resp.status_code, 200)
        rows = resp.json().get("results", [])
        row = next((item for item in rows if item["product_id"] == self.product.id), None)
        self.assertIsNotNone(row)
        self.assertEqual(row["findings_total"], 2)
        self.assertEqual(row["findings_active"], 1)
        self.assertEqual(row["severity"]["Critical"], 1)
        self.assertEqual(row["severity"]["Low"], 1)


class AISTPipelineSummaryTests(AISTApiBase):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.user)
        self.branch_version = AISTProjectVersion.objects.create(
            project=self.project,
            version_type=VersionType.GIT_BRANCH,
            version="release/main",
        )
        self.hash_version = AISTProjectVersion.objects.create(
            project=self.project,
            version_type=VersionType.GIT_HASH,
            version="deadbeef123",
            resolved_from_branch=self.branch_version,
        )
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
            branch_tag="main",
            commit_hash="abc123",
        )
        self.pipeline = AISTPipeline.objects.create(
            id="pipe-sum",
            project=self.project,
            status=AISTStatus.FINISHED,
            launch_data={"action_runs": [{"action_type": "export", "status": "performed"}]},
            project_version=self.hash_version,
        )
        self.pipeline.tests.add(self.test)
        Finding.objects.create(
            test=self.test,
            title="High Finding",
            severity="High",
            date=timezone.now(),
            reporter=self.user,
        )

    def test_pipeline_summary(self):
        resp = self.client.get(reverse("client_pipeline_summary"))
        self.assertEqual(resp.status_code, 200)
        results = resp.json().get("results", [])
        row = next((item for item in results if item["id"] == self.pipeline.id), None)
        self.assertIsNotNone(row)
        self.assertEqual(row["status"], AISTStatus.FINISHED)
        self.assertEqual(row["branch"], "release/main")
        self.assertEqual(row["commit"], "deadbeef123")
        self.assertEqual(row["findings"], 1)

    def test_pipeline_summary_filters_by_project_id(self):
        other_product = Product.objects.create(
            name="Other Summary Product",
            description="desc",
            prod_type=self.prod_type,
            sla_configuration_id=self.sla.id,
        )
        other_project = AISTProject.objects.create(
            product=other_product,
            supported_languages=["python"],
            script_path="scripts/build.sh",
            compilable=False,
            profile={},
        )
        other_pipeline = AISTPipeline.objects.create(
            id="pipe-sum-other",
            project=other_project,
            status=AISTStatus.FINISHED,
        )

        resp = self.client.get(reverse("client_pipeline_summary"), data={"project_id": self.project.id})
        self.assertEqual(resp.status_code, 200)
        results = resp.json().get("results", [])
        ids = {item["id"] for item in results}
        self.assertIn(self.pipeline.id, ids)
        self.assertNotIn(other_pipeline.id, ids)

    def test_pipeline_filter_findings(self):
        url = reverse("aist_api:finding_list")
        resp = self.client.get(url, data={"pipeline_id": self.pipeline.id})
        self.assertEqual(resp.status_code, 200)
        results = resp.data.get("results", [])
        self.assertEqual(len(results), 1)


class AISTUIApiTests(AISTApiBase):
    def test_project_product_is_unique(self):
        with self.assertRaises(IntegrityError):
            AISTProject.objects.create(
                product=self.product,
                supported_languages=["python"],
                script_path="scripts/another.sh",
                compilable=False,
                profile={},
            )

    def test_project_update_api(self):
        url = reverse("aist_api:project_detail", kwargs={"project_id": self.project.id})
        resp = self.client.post(
            url,
            data={
                "script_path": "scripts/new.sh",
                "supported_languages": "python, go",
                "profile": '{"paths": {"exclude": ["vendor/"]}}',
            },
        )
        self.assertEqual(resp.status_code, 200)
        self.project.refresh_from_db()
        self.assertEqual(self.project.script_path, "scripts/new.sh")

    def test_project_update_keeps_organization_when_not_provided(self):
        org = Organization.objects.create(name="Org Keep")
        org_pt = org.ensure_product_type()
        self.product.prod_type = org_pt
        self.product.save(update_fields=["prod_type"])
        Product_Type_Member.objects.create(
            product_type=org_pt,
            user=self.user,
            role=self.role_maintainer,
        )
        self.project.organization = org
        self.project.save(update_fields=["organization"])

        url = reverse("aist_api:project_detail", kwargs={"project_id": self.project.id})
        resp = self.client.post(
            url,
            data={
                "script_path": "scripts/new.sh",
                "supported_languages": "python",
            },
        )
        self.assertEqual(resp.status_code, 200)
        self.project.refresh_from_db()
        self.assertEqual(self.project.organization_id, org.id)

    def test_project_update_rejects_organization_with_mismatched_product_type(self):
        mismatch_org = Organization.objects.create(name="Org Mismatch")
        mismatch_pt = Product_Type.objects.create(name="Mismatch PT")
        mismatch_org.product_type = mismatch_pt
        mismatch_org.save(update_fields=["product_type"])
        mismatch_product = Product.objects.create(
            name="Mismatch Access Product",
            description="desc",
            prod_type=mismatch_pt,
            sla_configuration_id=self.sla.id,
        )
        Product_Type_Member.objects.create(
            product_type=mismatch_pt,
            user=self.user,
            role=self.role_maintainer,
        )
        AISTProject.objects.create(
            product=mismatch_product,
            supported_languages=["python"],
            script_path="scripts/build.sh",
            compilable=False,
            profile={},
            organization=mismatch_org,
        )

        url = reverse("aist_api:project_detail", kwargs={"project_id": self.project.id})
        resp = self.client.post(
            url,
            data={
                "script_path": "scripts/new.sh",
                "supported_languages": "python",
                "organization": mismatch_org.id,
            },
        )
        self.assertEqual(resp.status_code, 400)
        self.assertIn("ok", resp.data)
        self.assertFalse(resp.data["ok"])
        self.assertIn("organization", resp.data["errors"])

    def test_project_update_rejects_unauthorized_organization(self):
        hidden_org = Organization.objects.create(name="Org Hidden")
        hidden_pt = Product_Type.objects.create(name="Hidden PT")
        hidden_product = Product.objects.create(
            name="Hidden Product",
            description="desc",
            prod_type=hidden_pt,
            sla_configuration_id=self.sla.id,
        )
        AISTProject.objects.create(
            product=hidden_product,
            supported_languages=["python"],
            script_path="scripts/build.sh",
            compilable=False,
            profile={},
            organization=hidden_org,
        )

        url = reverse("aist_api:project_detail", kwargs={"project_id": self.project.id})
        resp = self.client.post(
            url,
            data={
                "script_path": "scripts/new.sh",
                "supported_languages": "python",
                "organization": hidden_org.id,
            },
        )
        self.assertEqual(resp.status_code, 400)
        self.assertTrue(
            "organization" in resp.data.get("errors", {}) or "organization" in resp.data,
        )

    def test_pipeline_stop_api(self):
        pipeline = AISTPipeline.objects.create(
            id="pipe-stop-1",
            project=self.project,
            project_version=self.pv,
            status=AISTStatus.SAST_LAUNCHED,
            run_task_id="celery-1",
        )
        url = reverse("aist_api:pipeline_stop", kwargs={"pipeline_id": pipeline.id})
        resp = self.client.post(url)
        self.assertEqual(resp.status_code, 200)
        pipeline.refresh_from_db()
        self.assertEqual(pipeline.status, AISTStatus.FINISHED)

    def test_send_request_to_ai_api_requires_waiting_status(self):
        pipeline = AISTPipeline.objects.create(
            id="pipe-ai-1",
            project=self.project,
            project_version=self.pv,
            status=AISTStatus.FINISHED,
        )
        url = reverse("aist_api:pipeline_send_request", kwargs={"pipeline_id": pipeline.id})
        resp = self.client.post(url, data={"finding_ids": []}, format="json")
        self.assertEqual(resp.status_code, 400)

    def test_finding_notes_api_returns_author_name_and_creates_note(self):
        engagement = Engagement.objects.create(
            name="Engage Notes API",
            target_start=timezone.now(),
            target_end=timezone.now(),
            product=self.product,
        )
        test_type = Test_Type.objects.create(name="Semgrep notes api")
        test = Test.objects.create(
            engagement=engagement,
            target_start=timezone.now(),
            target_end=timezone.now(),
            test_type=test_type,
        )
        finding = Finding.objects.create(
            test=test,
            title="Finding for notes API",
            severity="Low",
            date=timezone.now(),
            reporter=self.user,
        )

        create_resp = self.client.post(
            reverse("aist_api:finding_notes", kwargs={"finding_id": finding.id}),
            data={"entry": "new note"},
            format="json",
        )
        self.assertEqual(create_resp.status_code, 201)
        self.assertEqual(create_resp.data["entry"], "new note")
        self.assertEqual(create_resp.data["user_display"], self.user.username)

        list_resp = self.client.get(reverse("aist_api:finding_notes", kwargs={"finding_id": finding.id}))
        self.assertEqual(list_resp.status_code, 200)
        self.assertEqual(len(list_resp.data), 1)
        self.assertEqual(list_resp.data[0]["user_display"], self.user.username)
        self.assertTrue(Notes.objects.filter(id=create_resp.data["id"]).exists())

    def test_finding_export_api_exports_single_finding(self):
        engagement = Engagement.objects.create(
            name="Engage Finding Export",
            target_start=timezone.now(),
            target_end=timezone.now(),
            product=self.product,
        )
        test_type = Test_Type.objects.create(name="Semgrep finding export")
        test = Test.objects.create(
            engagement=engagement,
            target_start=timezone.now(),
            target_end=timezone.now(),
            test_type=test_type,
        )
        finding = Finding.objects.create(
            test=test,
            title="Finding for export API",
            severity="High",
            date=timezone.now(),
            reporter=self.user,
            file_path="src/app.py",
            line=9,
            description="Description before snippet\n```python\nprint('poc')\n```\nDescription after snippet",
        )
        pipeline = AISTPipeline.objects.create(
            id="pipe-export-single-finding",
            project=self.project,
            project_version=self.pv,
            status=AISTStatus.FINISHED,
        )
        ai_response = AISTAIResponse.objects.create(pipeline=pipeline, payload={"results": {}})
        AISTAIFindingResponse.objects.create(
            pipeline=pipeline,
            source_response=ai_response,
            finding=finding,
            verdict=AISTAIFindingResponse.Verdict.TRUE_POSITIVE,
            title="AI title export",
            summary="AI reasoning export",
            references=["example.com/advisory"],
        )

        resp = self.client.post(reverse("aist_api:finding_export", kwargs={"finding_id": finding.id}), data={})

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp["Content-Type"], "text/csv; charset=utf-8")
        self.assertIn(f"aist_finding_{finding.id}.csv", resp["Content-Disposition"])
        body = resp.content.decode("utf-8")
        self.assertIn("finding for export api", body.lower())
        self.assertIn("codeSnippet", body)
        self.assertIn("print('poc')", body)
        self.assertIn("AI TP", body)
        self.assertIn("https://example.com/advisory", body)

    def test_finding_export_api_rejects_unsupported_format(self):
        engagement = Engagement.objects.create(
            name="Engage Finding Export Invalid",
            target_start=timezone.now(),
            target_end=timezone.now(),
            product=self.product,
        )
        test_type = Test_Type.objects.create(name="Semgrep finding export invalid")
        test = Test.objects.create(
            engagement=engagement,
            target_start=timezone.now(),
            target_end=timezone.now(),
            test_type=test_type,
        )
        finding = Finding.objects.create(
            test=test,
            title="Finding for export invalid format",
            severity="Low",
            date=timezone.now(),
            reporter=self.user,
        )

        resp = self.client.post(
            reverse("aist_api:finding_export", kwargs={"finding_id": finding.id}),
            data={"format": "pdf"},
            format="json",
        )

        self.assertEqual(resp.status_code, 400)
        self.assertIn("format", resp.data)


class AISTSchemaTests(AISTApiBase):
    def test_openapi_includes_custom_aist_api_views(self):
        schema = SchemaGenerator().get_schema(request=None, public=True)
        paths = schema.get("paths", {})

        required_operations = {
            "/api/v2/aist/findings/": "get",
            "/api/v2/aist/findings/{finding_id}/notes/": "get",
            "/api/v2/aist/findings/{finding_id}/export/": "post",
            "/api/v2/aist/findings/tags/": "get",
            "/api/v2/aist/pipelines/{pipeline_id}/stop/": "post",
            "/api/v2/aist/pipelines/{pipeline_id}/export-ai-results/": "post",
        }
        for path, method in required_operations.items():
            self.assertIn(path, paths)
            self.assertIn(method, paths[path])
        self.assertIn("/api/v2/aist/projects/", paths)
        self.assertIn("post", paths["/api/v2/aist/projects/"])
        self.assertIn("/api/v2/aist/projects/{project_id}/", paths)
        self.assertIn("post", paths["/api/v2/aist/projects/{project_id}/"])
        self.assertIn("/api/v2/aist/projects/{project_id}/versions", paths)
        self.assertIn("post", paths["/api/v2/aist/projects/{project_id}/versions"])
        self.assertNotIn("/api/v2/aist/projects/create/", paths)
        self.assertNotIn("/api/v2/aist/projects/{project_id}/update/", paths)
        self.assertNotIn("/api/v2/aist/projects/{project_id}/versions/create/", paths)
        self.assertNotIn("/api/v2/aist/pipelines/summary/", paths)
        self.assertNotIn("/api/v2/aist/products/summary/", paths)


class LaunchConfigAPITests(AISTApiBase):
    def _list_create_url(self):
        return reverse("aist_api:project_launch_config_list_create", kwargs={"project_id": self.project.id})

    def _detail_url(self, cfg_id: int):
        return reverse(
            "aist_api:project_launch_config_detail",
            kwargs={"project_id": self.project.id, "config_id": cfg_id},
        )

    def _start_url(self, cfg_id: int):
        # api_urls.py: .../start/
        return reverse(
            "aist_api:project_launch_config_start",
            kwargs={"project_id": self.project.id, "config_id": cfg_id},
        )

    def _dashboard_url(self):
        return reverse("aist_api:launch_config_dashboard_list")

    def test_delete_launch_config(self):
        cfg = AISTProjectLaunchConfig.objects.create(
            project=self.project,
            name="Preset",
            description="",
            params={"project_version": {"id": self.pv.id}},
            is_default=False,
        )
        url = self._detail_url(cfg.id)
        resp = self.client.delete(url)
        self.assertEqual(resp.status_code, 204)
        self.assertFalse(AISTProjectLaunchConfig.objects.filter(id=cfg.id).exists())

    @patch("aist.api.launch_configs.PipelineArguments.normalize_params")
    def test_update_launch_config_params(self, mock_normalize):
        cfg = AISTProjectLaunchConfig.objects.create(
            project=self.project,
            name="Preset",
            description="",
            params={"project_version": {"id": self.pv.id}},
            is_default=False,
        )
        mock_normalize.return_value = {
            "project_version": {"id": self.pv.id},
            "ai_mode": "AUTO_DEFAULT",
        }

        resp = self.client.patch(
            self._detail_url(cfg.id),
            data={"params": {"ai_mode": "AUTO_DEFAULT"}},
            format="json",
        )

        self.assertEqual(resp.status_code, 200)
        cfg.refresh_from_db()
        self.assertEqual(cfg.params, mock_normalize.return_value)
        mock_normalize.assert_called_once_with(project=self.project, raw_params={"ai_mode": "AUTO_DEFAULT"})

    @patch("aist.api.launch_configs.PipelineArguments.normalize_params")
    def test_create_launch_config_normalizes_and_strips_project_fields(self, mock_normalize):
        mock_normalize.return_value = {
            "project_id": self.project.id,
            "project_version": {"id": self.pv.id},
            "log_level": "INFO",
            "ai_mode": "AUTO_DEFAULT",
        }

        resp = self.client.post(
            self._list_create_url(),
            data={
                "name": "My preset",
                "is_default": True,
                "params": {"log_level": "INFO"},
            },
            format="json",
        )

        self.assertEqual(resp.status_code, 201)

        cfg = AISTProjectLaunchConfig.objects.get(id=resp.data["id"])

        self.assertEqual(cfg.params["log_level"], "INFO")
        self.assertIn("project_version", cfg.params)

    @patch("aist.api.launch_configs.PipelineArguments.normalize_params")
    def test_create_default_launch_config_unsets_previous_default(self, mock_normalize):
        mock_normalize.return_value = {"log_level": "INFO"}

        # create first default
        r1 = self.client.post(
            self._list_create_url(),
            data={"name": "Preset 1", "is_default": True, "params": {"log_level": "INFO"}},
            format="json",
        )
        self.assertEqual(r1.status_code, 201)
        cfg1_id = r1.data["id"]

        # create second default -> first should be unset :contentReference[oaicite:8]{index=8}
        r2 = self.client.post(
            self._list_create_url(),
            data={"name": "Preset 2", "is_default": True, "params": {"log_level": "INFO"}},
            format="json",
        )
        self.assertEqual(r2.status_code, 201)
        cfg2_id = r2.data["id"]

        cfg1 = AISTProjectLaunchConfig.objects.get(id=cfg1_id)
        cfg2 = AISTProjectLaunchConfig.objects.get(id=cfg2_id)

        self.assertFalse(cfg1.is_default)
        self.assertTrue(cfg2.is_default)

    def test_launch_config_detail_masks_sensitive_params(self):
        cfg = AISTProjectLaunchConfig.objects.create(
            project=self.project,
            name="Preset Secret",
            description="",
            params={"env": {"API_TOKEN": "plain-token", "NORMAL": "ok"}},
            is_default=False,
        )

        resp = self.client.get(self._detail_url(cfg.id))

        self.assertEqual(resp.status_code, 200)
        env = resp.data.get("params", {}).get("env", {})
        self.assertEqual(env.get("API_TOKEN"), MASKED_VALUE)
        self.assertEqual(env.get("NORMAL"), "ok")

    def test_launch_config_dashboard_masks_sensitive_params(self):
        AISTProjectLaunchConfig.objects.create(
            project=self.project,
            name="Preset Secret Dashboard",
            description="",
            params={"env": {"PRIVATE_TOKEN": "plain-token"}},
            is_default=False,
        )

        resp = self.client.get(self._dashboard_url())

        self.assertEqual(resp.status_code, 200)
        results = resp.data if isinstance(resp.data, list) else resp.data.get("results", [])
        self.assertTrue(results)
        self.assertEqual(results[0].get("params", {}).get("env", {}).get("PRIVATE_TOKEN"), MASKED_VALUE)

    @patch("aist.api.launch_configs.run_sast_pipeline")
    @patch("aist.api.launch_configs.PipelineArguments.normalize_params")
    @patch("aist.api.launch_configs.has_unfinished_pipeline", return_value=False)
    def test_start_by_launch_config_uses_latest_version_and_merges_overrides(
            self,
            mock_has_unfinished,
            mock_normalize,
            mock_run_task,
    ):
        # Create a "latest" version that should be chosen when project_version_id omitted :contentReference[oaicite:9]{index=9}
        AISTProjectVersion.objects.create(
            project=self.project,
            version_type=VersionType.GIT_HASH,
            version="develop",
        )

        cfg = AISTProjectLaunchConfig.objects.create(
            project=self.project,
            name="Preset",
            description="",
            params={"log_level": "INFO", "rebuild_images": False},
            is_default=False,
        )

        mock_normalize.return_value = {"project_version": {"id": self.pv.id}}
        mock_run_task.delay.return_value = SimpleNamespace(id="celery-999")

        resp = self.client.post(
            self._start_url(cfg.id),
            data={"params": {"rebuild_images": True}},
            format="json",
        )
        self.assertEqual(resp.status_code, 201)
        mock_has_unfinished.assert_called_once()

        # Ensure normalize got merged raw_params
        _, kwargs = mock_normalize.call_args
        self.assertEqual(kwargs["project"], self.project)
        self.assertEqual(kwargs["raw_params"]["log_level"], "INFO")
        self.assertEqual(kwargs["raw_params"]["rebuild_images"], True)

        mock_run_task.delay.assert_called_once()
