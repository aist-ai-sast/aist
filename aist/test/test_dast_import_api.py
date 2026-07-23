"""
Tests for the generic report-import API endpoints (auth, org-scope, multipart, async),
exercised with a DAST report as the concrete example scan_type. ``validate/`` is the
endpoint that actually calls a parser and can reject bad *content* synchronously;
``import/`` (confirm) only gates ``scan_type``/``commit_hash`` shape — a malformed report
body now surfaces as a failed pipeline, not a 400, since format validation is the
registered parser's job at real-import time (see test_dast_import_task.py).
"""
from __future__ import annotations

import hashlib
import json
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.core.files.storage import default_storage
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, TestCase
from django.urls import reverse
from dojo.authorization.roles_permissions import Roles
from dojo.models import Product, Product_Type, Product_Type_Member, Role

from aist.models import AISTPipeline, AISTProject, AISTStatus, RepositoryInfo, ScmType
from aist.parser_overrides import DAST_SCAN_TYPE

SHA = "fd5b25aa1234567890abcdef1234567890abcdef"


def _report_payload(*, missing_description: bool = False) -> dict:
    finding = {
        "title": "Cross-tenant BOLA on subscription keys",
        "severity": "High",
        "unique_id_from_tool": "BOLA-cross-cp-cross-tenant-bola",
        "vuln_id_from_tool": "BOLA-cross-cp-cross-tenant-bola",
    }
    if not missing_description:
        finding["description"] = "redacted description"
    return {
        "name": "DAST",
        "type": DAST_SCAN_TYPE,
        "version": "v1",
        "findings": [finding],
        "dast_run_metadata": {"run_id": "run-123", "source_commits": {"cloud_portal": SHA}},
    }


def _upload(payload: object, name: str = "generic-aist-report.json") -> SimpleUploadedFile:
    return SimpleUploadedFile(name, json.dumps(payload).encode(), content_type="application/json")


class PipelineImportAPITests(TestCase):
    def setUp(self):
        # The throttle cache persists across test methods within the same process,
        # so a prior test's requests could push this test straight to a spurious 429.
        cache.clear()
        self.user = get_user_model().objects.create_user(username="report-import-user", password="pass")  # noqa: S106
        self.prod_type = Product_Type.objects.create(name="Report Import PT")
        role_maintainer, _ = Role.objects.get_or_create(id=Roles.Maintainer, defaults={"name": "Maintainer"})
        Product_Type_Member.objects.create(product_type=self.prod_type, user=self.user, role=role_maintainer)

        self.product = Product.objects.create(name="Report Import Product", description="desc", prod_type=self.prod_type)
        repository = RepositoryInfo.objects.create(type=ScmType.GITHUB, repo_owner="acme", repo_name="cloud_portal")
        self.project = AISTProject.objects.create(
            product=self.product,
            supported_languages=[],
            compilable=False,
            profile={},
            repository=repository,
        )

        self.client.force_login(self.user)

    def _validate_url(self):
        return reverse("aist_api:aist_pipeline_import_validate")

    def _import_url(self):
        return reverse("aist_api:aist_pipeline_import")

    def test_routes_resolve(self):
        self.assertTrue(self._validate_url().endswith("/pipelines/import/validate/"))
        self.assertTrue(self._import_url().endswith("/pipelines/import/"))

    def test_requires_authentication(self):
        anon = Client()
        for url, extra in (
            (self._validate_url(), {"scan_type": DAST_SCAN_TYPE}),
            (self._import_url(), {"scan_type": DAST_SCAN_TYPE, "commit_hash": SHA}),
        ):
            response = anon.post(url, {"file": _upload(_report_payload()), "project_id": self.project.id, **extra})
            self.assertIn(response.status_code, {401, 403})

    def test_denies_user_without_project_access(self):
        other_pt = Product_Type.objects.create(name="Other PT")
        other_product = Product.objects.create(name="Other Product", description="desc", prod_type=other_pt)
        other_repo = RepositoryInfo.objects.create(type=ScmType.GITHUB, repo_owner="acme", repo_name="other")
        other_project = AISTProject.objects.create(
            product=other_product,
            supported_languages=[],
            compilable=False,
            profile={},
            repository=other_repo,
        )
        response = self.client.post(
            self._import_url(),
            {
                "file": _upload(_report_payload()),
                "project_id": other_project.id,
                "scan_type": DAST_SCAN_TYPE,
                "commit_hash": SHA,
            },
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("project_id", response.data)
        self.assertFalse(AISTPipeline.objects.filter(project=other_project).exists())

    def test_validate_returns_preview_with_detected_commit_hash(self):
        response = self.client.post(
            self._validate_url(),
            {"file": _upload(_report_payload()), "project_id": self.project.id, "scan_type": DAST_SCAN_TYPE},
        )
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.data["findings_count"], 1)
        self.assertEqual(response.data["severity_breakdown"], {"High": 1})
        self.assertEqual(response.data["detected_commit_hash"], SHA)

    def test_validate_rejects_malformed_finding_without_re_reading_the_file_elsewhere(self):
        response = self.client.post(
            self._validate_url(),
            {
                "file": _upload(_report_payload(missing_description=True)),
                "project_id": self.project.id,
                "scan_type": DAST_SCAN_TYPE,
            },
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("description", str(response.data))

    def test_validate_rejects_unregistered_scan_type(self):
        response = self.client.post(
            self._validate_url(),
            {"file": _upload(_report_payload()), "project_id": self.project.id, "scan_type": "Not A Real Parser"},
        )
        self.assertEqual(response.status_code, 400)

    def test_validate_rejects_non_object_report_root(self):
        response = self.client.post(
            self._validate_url(),
            {"file": _upload([]), "project_id": self.project.id, "scan_type": DAST_SCAN_TYPE},
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("JSON object", str(response.data))

    def test_validate_rejects_invalid_source_commits_shape(self):
        payload = _report_payload()
        payload["dast_run_metadata"]["source_commits"] = []
        response = self.client.post(
            self._validate_url(),
            {"file": _upload(payload), "project_id": self.project.id, "scan_type": DAST_SCAN_TYPE},
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("source_commits", str(response.data))

    def test_validate_persists_nothing(self):
        before = set(default_storage.listdir("report_imports")[1]) if default_storage.exists("report_imports") else set()
        self.client.post(
            self._validate_url(),
            {"file": _upload(_report_payload()), "project_id": self.project.id, "scan_type": DAST_SCAN_TYPE},
        )
        after = set(default_storage.listdir("report_imports")[1]) if default_storage.exists("report_imports") else set()
        self.assertEqual(before, after)

    @patch("aist.api.report_import.import_report.apply_async")
    @patch("aist.api.report_import.uuid4")
    def test_valid_import_returns_202_and_creates_pipeline(self, mock_uuid4, mock_apply_async):
        mock_uuid4.return_value.hex = "celery-task-id"
        response = self.client.post(
            self._import_url(),
            {
                "file": _upload(_report_payload()),
                "project_id": self.project.id,
                "scan_type": DAST_SCAN_TYPE,
                "commit_hash": SHA,
            },
        )
        self.assertEqual(response.status_code, 202, response.content)
        self.assertIn("pipeline_id", response.data)
        self.assertEqual(response.data["run_task_id"], "celery-task-id")

        pipeline = AISTPipeline.objects.get(id=response.data["pipeline_id"])
        self.assertEqual(pipeline.project_id, self.project.id)
        self.assertEqual(pipeline.status, AISTStatus.FINISHED)
        mock_apply_async.assert_called_once()
        task_args = mock_apply_async.call_args.kwargs["args"]
        self.assertEqual(mock_apply_async.call_args.kwargs["task_id"], "celery-task-id")
        self.assertEqual(task_args[7], hashlib.sha256(json.dumps(_report_payload()).encode()).hexdigest())
        self.addCleanup(default_storage.delete, task_args[1])

        status_response = self.client.get(
            reverse("aist_api:pipeline_status", kwargs={"pipeline_id": pipeline.id}),
        )
        self.assertEqual(status_response.status_code, 200)
        self.assertEqual(status_response.data["run_task_id"], "celery-task-id")

    @patch("aist.api.report_import.import_report.apply_async", side_effect=RuntimeError("broker unavailable"))
    @patch("aist.api.report_import.uuid4")
    def test_dispatch_failure_cleans_upload_and_pipeline(self, mock_uuid4, mock_apply_async):
        mock_uuid4.return_value.hex = "failed-task-id"
        before_files = set(
            default_storage.listdir("report_imports")[1]
            if default_storage.exists("report_imports")
            else [],
        )
        before_pipelines = AISTPipeline.objects.filter(project=self.project).count()

        response = self.client.post(
            self._import_url(),
            {
                "file": _upload(_report_payload()),
                "project_id": self.project.id,
                "scan_type": DAST_SCAN_TYPE,
                "commit_hash": SHA,
            },
        )
        self.assertEqual(response.status_code, 500)
        mock_apply_async.assert_called_once()

        after_files = set(
            default_storage.listdir("report_imports")[1]
            if default_storage.exists("report_imports")
            else [],
        )
        self.assertEqual(after_files, before_files)
        self.assertEqual(AISTPipeline.objects.filter(project=self.project).count(), before_pipelines)

    def test_import_with_project_lacking_repository_returns_400_and_creates_no_pipeline(self):
        no_repo_product = Product.objects.create(
            name="Report Import Product (no repo)", description="desc", prod_type=self.prod_type,
        )
        no_repo_project = AISTProject.objects.create(
            product=no_repo_product,
            supported_languages=[],
            compilable=False,
            profile={},
        )
        before = AISTPipeline.objects.filter(project=no_repo_project).count()
        response = self.client.post(
            self._import_url(),
            {
                "file": _upload(_report_payload()),
                "project_id": no_repo_project.id,
                "scan_type": DAST_SCAN_TYPE,
                "commit_hash": SHA,
            },
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(AISTPipeline.objects.filter(project=no_repo_project).count(), before)

    def test_import_endpoint_is_throttled(self):
        # Both import endpoints carry a shared ScopedRateThrottle ("aist_pipeline_import",
        # 20/hour default) — this is an abusable action (fans out to DefaultImporter +
        # Celery, creates DB rows and a storage write per call) with no other limit.
        # Omitting commit_hash 400s fast, before any import work happens, so this stays cheap.
        statuses = [
            self.client.post(
                self._import_url(),
                {"file": _upload(_report_payload()), "project_id": self.project.id, "scan_type": DAST_SCAN_TYPE},
            ).status_code
            for _ in range(25)
        ]
        self.assertIn(429, statuses)
        self.assertTrue(all(code in {400, 429} for code in statuses))
