"""
Tests for the generic report-import API endpoints (auth, org-scope, multipart, async),
exercised with a DAST report as the concrete example scan_type. ``validate/`` is the
endpoint that actually calls a parser and can reject bad *content* synchronously;
DAST preview and confirm both require an explicit binding and a complete v2 terminal
artifact. The worker reparses the persisted artifact before finalization.
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
from django.utils import timezone
from dojo.authorization.roles_permissions import Roles
from dojo.models import Product, Product_Type, Product_Type_Member, Role, SLA_Configuration

from aist.models import (
    AISTPipeline,
    AISTProject,
    AISTStatus,
    DastProjectBinding,
    DastTarget,
    Organization,
    OrgIntegration,
    OrgIntegrationType,
    PipelineExecutionType,
    RepositoryInfo,
    ScmType,
)
from aist.parser_overrides import DAST_SCAN_TYPE
from aist.test import dast_fixtures

SHA = "fd5b25aa1234567890abcdef1234567890abcdef"


def _report_payload(*, missing_description: bool = False, run_metadata: dict | None = None) -> dict:
    """The artifact `dast export-findings --format generic-aist` writes — what an operator uploads."""
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
        "version": "backend@fd5b25aa1234",
        "findings": [finding],
        "dast_run_metadata": {
            "run_id": "run-123",
            "target": "cloud-app",
            "stand": "qa-1",
            "source_commits": {"backend": SHA},
            **(run_metadata or {}),
        },
    }


def _upload(payload: object, name: str = "generic-aist-report.json") -> SimpleUploadedFile:
    return SimpleUploadedFile(name, json.dumps(payload).encode(), content_type="application/json")


class PipelineImportAPITests(TestCase):
    def setUp(self):
        # The throttle cache persists across test methods within the same process,
        # so a prior test's requests could push this test straight to a spurious 429.
        cache.clear()
        self.user = get_user_model().objects.create_user(username="report-import-user", password="pass")  # noqa: S106
        self.sla = SLA_Configuration.objects.create(name="Report Import SLA")
        self.prod_type = Product_Type.objects.create(name="Report Import PT")
        self.organization = Organization.objects.create(name="Report Import Org", product_type=self.prod_type)
        role_maintainer, _ = Role.objects.get_or_create(id=Roles.Maintainer, defaults={"name": "Maintainer"})
        Product_Type_Member.objects.create(product_type=self.prod_type, user=self.user, role=role_maintainer)

        self.product = Product.objects.create(
            name="Report Import Product",
            description="desc",
            prod_type=self.prod_type,
            sla_configuration=self.sla,
        )
        repository = RepositoryInfo.objects.create(type=ScmType.GITHUB, repo_owner="acme", repo_name="cloud_portal")
        self.project = AISTProject.objects.create(
            product=self.product,
            supported_languages=[],
            compilable=False,
            profile={},
            repository=repository,
        )
        integration = OrgIntegration.objects.create(
            organization=self.organization,
            integration_type=OrgIntegrationType.DAST,
            name="Report Import DAST",
            is_active=True,
        )
        target = DastTarget.objects.create(
            integration=integration,
            provider_id="cloud-app",
            display_name="Cloud app",
            contract_revision="2.0",
            capability_revision="sha256:import-capability",
            schema_digest="sha256:import-schema",
            parameter_schema={
                "$schema": "https://json-schema.org/draft/2020-12/schema",
                "type": "object",
                "additionalProperties": False,
            },
            provider_defaults={},
            repository_keys=["backend"],
            launch_requirements=["repository-trigger"],
            autonomous_ready=True,
            last_seen_at=timezone.now(),
        )
        self.binding = DastProjectBinding.objects.create(
            project=self.project,
            target=target,
            source_repo_key="backend",
            enabled=True,
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
            (self._import_url(), {"scan_type": DAST_SCAN_TYPE, "binding_id": self.binding.id}),
        ):
            response = anon.post(url, {"file": _upload(_report_payload()), "project_id": self.project.id, **extra})
            self.assertIn(response.status_code, {401, 403})

    def test_denies_user_without_project_access(self):
        other_pt = Product_Type.objects.create(name="Other PT")
        other_product = Product.objects.create(
            name="Other Product",
            description="desc",
            prod_type=other_pt,
            sla_configuration=self.sla,
        )
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
                "binding_id": self.binding.id,
            },
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("project_id", response.data)
        self.assertFalse(AISTPipeline.objects.filter(project=other_project).exists())

    def test_validate_returns_preview_with_actual_source_commit(self):
        response = self.client.post(
            self._validate_url(),
            {
                "file": _upload(_report_payload()),
                "project_id": self.project.id,
                "scan_type": DAST_SCAN_TYPE,
                "binding_id": self.binding.id,
            },
        )
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.data["findings_count"], 1)
        self.assertEqual(response.data["severity_breakdown"], {"High": 1})
        self.assertEqual(response.data["actual_source_commit"], SHA)

    def test_validate_rejects_malformed_finding_without_re_reading_the_file_elsewhere(self):
        response = self.client.post(
            self._validate_url(),
            {
                "file": _upload(_report_payload(missing_description=True)),
                "project_id": self.project.id,
                "scan_type": DAST_SCAN_TYPE,
                "binding_id": self.binding.id,
            },
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("Required fields are missing", str(response.data))

    def test_validate_rejects_unregistered_scan_type(self):
        response = self.client.post(
            self._validate_url(),
            {"file": _upload(_report_payload()), "project_id": self.project.id, "scan_type": "Not A Real Parser"},
        )
        self.assertEqual(response.status_code, 400)

    def test_validate_rejects_non_object_report_root(self):
        response = self.client.post(
            self._validate_url(),
            {
                "file": _upload([]),
                "project_id": self.project.id,
                "scan_type": DAST_SCAN_TYPE,
                "binding_id": self.binding.id,
            },
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("JSON object", str(response.data))

    def test_validate_rejects_invalid_source_commits_shape(self):
        payload = _report_payload()
        payload["dast_run_metadata"]["source_commits"] = []
        response = self.client.post(
            self._validate_url(),
            {
                "file": _upload(payload),
                "project_id": self.project.id,
                "scan_type": DAST_SCAN_TYPE,
                "binding_id": self.binding.id,
            },
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("source_commits", str(response.data))

    def test_validate_persists_nothing(self):
        before = set(default_storage.listdir("report_imports")[1]) if default_storage.exists("report_imports") else set()
        self.client.post(
            self._validate_url(),
            {
                "file": _upload(_report_payload()),
                "project_id": self.project.id,
                "scan_type": DAST_SCAN_TYPE,
                "binding_id": self.binding.id,
            },
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
                "binding_id": self.binding.id,
            },
        )
        self.assertEqual(response.status_code, 202, response.content)
        self.assertIn("pipeline_id", response.data)
        self.assertEqual(response.data["run_task_id"], "celery-task-id")

        pipeline = AISTPipeline.objects.get(id=response.data["pipeline_id"])
        self.assertEqual(pipeline.project_id, self.project.id)
        self.assertEqual(pipeline.execution_type, PipelineExecutionType.MANUAL_IMPORT)
        self.assertEqual(pipeline.status, AISTStatus.ADMITTED)
        mock_apply_async.assert_called_once()
        task_args = mock_apply_async.call_args.kwargs["args"]
        self.assertEqual(mock_apply_async.call_args.kwargs["task_id"], "celery-task-id")
        self.assertEqual(task_args[7], hashlib.sha256(json.dumps(_report_payload()).encode()).hexdigest())
        self.assertEqual(task_args[8], self.binding.id)
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
                "binding_id": self.binding.id,
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
            name="Report Import Product (no repo)",
            description="desc",
            prod_type=self.prod_type,
            sla_configuration=self.sla,
        )
        no_repo_project = AISTProject.objects.create(
            product=no_repo_product,
            supported_languages=[],
            compilable=False,
            profile={},
        )
        no_repo_binding = DastProjectBinding.objects.create(
            project=no_repo_project,
            target=self.binding.target,
            source_repo_key="backend",
            enabled=True,
        )
        before = AISTPipeline.objects.filter(project=no_repo_project).count()
        response = self.client.post(
            self._import_url(),
            {
                "file": _upload(_report_payload()),
                "project_id": no_repo_project.id,
                "scan_type": DAST_SCAN_TYPE,
                "binding_id": no_repo_binding.id,
            },
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(AISTPipeline.objects.filter(project=no_repo_project).count(), before)

    def test_import_endpoint_is_throttled(self):
        # Both import endpoints carry a shared ScopedRateThrottle ("aist_pipeline_import",
        # 20/hour default) — this is an abusable action (fans out to DefaultImporter +
        # Celery, creates DB rows and a storage write per call) with no other limit.
        # Omitting binding_id 400s fast, before any import work happens, so this stays cheap.
        statuses = [
            self.client.post(
                self._import_url(),
                {"file": _upload(_report_payload()), "project_id": self.project.id, "scan_type": DAST_SCAN_TYPE},
            ).status_code
            for _ in range(25)
        ]
        self.assertIn(429, statuses)
        self.assertTrue(all(code in {400, 429} for code in statuses))


class DastImportPreviewRunMetadataTests(PipelineImportAPITests):

    """The operator sees what the run covered before committing the import."""

    # Real values from run 80c744a2be37d91c07a7a8ef97c520be.
    COVERAGE = {
        "unit": "endpoint",
        "discovered": 784,
        "reachable": 176,
        "analysed": 38,
        "planned": 10,
        "analysed_names": ["analytics3-test-hdw-mx", "cloud-prod-hdw-mx"],
        "beyond_plan_names": ["cloud-prod-hdw-mx"],
    }
    TOKEN_USAGE = {
        "total": {
            "input": 2234,
            "output": 951808,
            "thinking": 331554,
            "cache_creation": 2578204,
            "cache_read": 90024238,
            "calls": 1117,
        },
    }

    def _preview(self, payload):
        response = self.client.post(
            self._validate_url(),
            {
                "file": _upload(payload),
                "project_id": self.project.id,
                "scan_type": DAST_SCAN_TYPE,
                "binding_id": self.binding.id,
            },
        )
        self.assertEqual(response.status_code, 200, response.content)
        return response.data

    def test_preview_reports_the_coverage_and_spend_the_report_carries(self):
        data = self._preview(
            _report_payload(run_metadata={"coverage": self.COVERAGE, "token_usage": self.TOKEN_USAGE}),
        )

        self.assertEqual(data["findings_count"], 1)
        run = data["dast_run"]
        self.assertEqual(run["analysed"], 38)
        self.assertEqual(run["reachable"], 176)
        self.assertEqual(run["beyond_plan"], 1)
        self.assertEqual(run["model_calls"], 1117)
        self.assertEqual(run["total_tokens"], 2234 + 951808 + 2578204 + 90024238)

    def test_preview_of_a_report_without_the_blocks_reports_no_counters(self):
        run = self._preview(_report_payload())["dast_run"]

        self.assertEqual(run["run_id"], "run-123")
        self.assertIsNone(run["analysed"])
        self.assertIsNone(run["total_tokens"])


class DastPerimeterImportTests(TestCase):

    """
    A perimeter target reports no source revision, so its project needs no repository.

    Regression: the import serializer demanded a linked project repository for *every* DAST import.
    A sourceless target's findings attach to the version standing for the target itself, so that
    requirement locked every perimeter report out of a project that legitimately has none — the
    operator saw "DAST imports require a linked project repository" and could go no further.
    """

    def setUp(self):
        cache.clear()
        self.user = get_user_model().objects.create_user(username="perimeter-import-user", password="pass")  # noqa: S106
        sla = SLA_Configuration.objects.create(name="Perimeter Import SLA")
        prod_type = Product_Type.objects.create(name="Perimeter Import PT")
        organization = Organization.objects.create(name="Perimeter Import Org", product_type=prod_type)
        role_maintainer, _ = Role.objects.get_or_create(id=Roles.Maintainer, defaults={"name": "Maintainer"})
        Product_Type_Member.objects.create(product_type=prod_type, user=self.user, role=role_maintainer)
        product = Product.objects.create(
            name="Perimeter Import Product",
            description="desc",
            prod_type=prod_type,
            sla_configuration=sla,
        )
        # Deliberately no RepositoryInfo: a perimeter scan has no source to link to.
        self.project = AISTProject.objects.create(
            product=product,
            supported_languages=[],
            compilable=False,
            profile={},
        )
        integration, _state = dast_fixtures.create_dast_integration(
            organization=organization,
            public_id="perimeter-import",
            name="Perimeter Import DAST",
        )
        source_target, perimeter_target = dast_fixtures.create_dast_targets(
            integration=integration,
            wires=(dast_fixtures.target_wire("cloud-app"), dast_fixtures.perimeter_target_wire("perimeter")),
        )
        self.source_binding = dast_fixtures.create_dast_binding(project=self.project, target=source_target)
        self.binding = dast_fixtures.create_dast_binding(project=self.project, target=perimeter_target)
        self.client.force_login(self.user)

    def _perimeter_report(self) -> dict:
        report = _report_payload()
        report["dast_run_metadata"].update(
            run_id="run-perimeter-1",
            target="perimeter",
            source_commits={},
        )
        report["dast_run_metadata"].pop("stand", None)
        return report

    def _post(self, url, report, *, binding):
        return self.client.post(
            url,
            {
                "file": _upload(report),
                "project_id": self.project.id,
                "scan_type": DAST_SCAN_TYPE,
                "binding_id": binding.id,
            },
        )

    def test_the_binding_needs_no_repository_when_its_target_reports_no_source(self):
        self.assertFalse(self.binding.requires_source_repository)
        self.assertTrue(self.source_binding.requires_source_repository)
        self.assertIsNone(self.project.repository_id)

    def test_preview_accepts_a_perimeter_report_from_a_project_without_a_repository(self):
        response = self._post(
            reverse("aist_api:aist_pipeline_import_validate"), self._perimeter_report(), binding=self.binding,
        )

        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.data["findings_count"], 1)
        self.assertIsNone(response.data["actual_source_commit"])

    @patch("aist.api.report_import.import_report.apply_async")
    def test_import_accepts_a_perimeter_report_from_a_project_without_a_repository(self, mock_apply_async):
        response = self._post(
            reverse("aist_api:aist_pipeline_import"), self._perimeter_report(), binding=self.binding,
        )

        self.assertEqual(response.status_code, 202, response.content)
        pipeline = AISTPipeline.objects.get(pk=response.data["pipeline_id"])
        self.assertEqual(pipeline.project_id, self.project.id)
        self.assertEqual(pipeline.execution_type, PipelineExecutionType.MANUAL_IMPORT)
        self.assertEqual(mock_apply_async.call_count, 1)

    def test_a_source_bound_target_still_needs_the_project_to_have_a_repository(self):
        """The requirement is not gone; it is asked of the binding that actually has it."""
        response = self._post(
            reverse("aist_api:aist_pipeline_import_validate"), _report_payload(), binding=self.source_binding,
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("project_id", response.data)
