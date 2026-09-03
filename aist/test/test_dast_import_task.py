"""
Integration tests for the manual-report-import Celery task's strict DAST branch. These
cover persisted-artifact verification and the shared DAST finalization path.
"""
from __future__ import annotations

import hashlib
import json

from django.contrib.auth import get_user_model
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.test import TestCase
from django.utils import timezone
from dojo.models import Product, Product_Type, SLA_Configuration

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
    VersionType,
)
from aist.parser_overrides import DAST_SCAN_TYPE
from aist.tasks.report_import import import_report

SHA = "fd5b25aa1234567890abcdef1234567890abcdef"


def _report_payload() -> dict:
    return {
        "name": "DAST",
        "type": "DAST Autonomous Scan",
        "version": "backend@fd5b25aa1234",
        "findings": [
            {
                "title": "Cross-tenant BOLA on subscription keys",
                "severity": "High",
                "description": "redacted description",
                "unique_id_from_tool": "BOLA-cross-cp-cross-tenant-bola",
                "vuln_id_from_tool": "BOLA-cross-cp-cross-tenant-bola",
                "cwe": 639,
                "dynamic_finding": True,
                "endpoints": ["https://api.example.com/v1/subscriptions/123"],
            },
        ],
        "dast_run_metadata": {
            "run_id": "run-123",
            "target": "cloud-app",
            "stand": "qa-1",
            "source_commits": {"backend": SHA},
        },
    }


class ImportReportTaskTests(TestCase):
    def setUp(self):
        prod_type = Product_Type.objects.create(name="Report Import Task PT")
        organization = Organization.objects.create(name="Report Import Task Org", product_type=prod_type)
        product = Product.objects.create(
            name="Report Import Task Product",
            description="desc",
            prod_type=prod_type,
            sla_configuration=SLA_Configuration.objects.create(name="Report Import Task SLA"),
        )
        repository = RepositoryInfo.objects.create(
            type=ScmType.GITHUB,
            repo_owner="acme",
            repo_name="cloud_portal",
        )
        self.project = AISTProject.objects.create(
            product=product,
            supported_languages=[],
            compilable=False,
            profile={},
            repository=repository,
        )
        integration = OrgIntegration.objects.create(
            organization=organization,
            integration_type=OrgIntegrationType.DAST,
            name="Report Import Task DAST",
            is_active=True,
        )
        target = DastTarget.objects.create(
            integration=integration,
            provider_id="cloud-app",
            display_name="Cloud app",
            contract_revision="2.0",
            capability_revision="sha256:task-capability",
            schema_digest="sha256:task-schema",
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
        self.uploader = get_user_model().objects.create_user(username="report-importer")
        self.pipeline = AISTPipeline.objects.create(
            id="importtest1",
            project=self.project,
            dast_binding=self.binding,
            execution_type=PipelineExecutionType.MANUAL_IMPORT,
            status=AISTStatus.ADMITTED,
            launch_data={
                "source": "manual_import",
                "scan_type": DAST_SCAN_TYPE,
                "uploader_id": self.uploader.id,
                "filename": "generic-aist-report.json",
                "dast_binding_id": self.binding.id,
            },
        )
        self.report_bytes = json.dumps(_report_payload()).encode()
        self.report_sha256 = hashlib.sha256(self.report_bytes).hexdigest()
        self.storage_name = default_storage.save(
            "report_imports/importtest1.json",
            ContentFile(self.report_bytes),
        )

    def test_import_report_accepts_async_user_contract_and_deletes_temp_file(self):
        # The shared finalizer leaves the pipeline at UPLOADING_RESULTS until its
        # transaction.on_commit hand-off advances it to deduplication. TestCase's
        # outer transaction deliberately does not execute that callback; the
        # callback behavior is covered by test_dast_finalization.
        import_report.run(
            pipeline_id=self.pipeline.id,
            storage_name=self.storage_name,
            project_id=self.project.id,
            uploader_id=self.uploader.id,
            scan_type=DAST_SCAN_TYPE,
            commit_hash=SHA,
            filename="generic-aist-report.json",
            sha256=self.report_sha256,
            binding_id=self.binding.id,
            async_user=self.uploader,
        )

        pipeline = AISTPipeline.objects.get(id=self.pipeline.id)
        self.assertEqual(pipeline.status, AISTStatus.UPLOADING_RESULTS)
        self.assertIsNotNone(pipeline.project_version)
        self.assertEqual(pipeline.project_version.version, SHA)
        self.assertEqual(pipeline.project_version.version_type, VersionType.GIT_HASH)
        self.assertGreaterEqual(pipeline.tests.count(), 1)
        self.assertGreaterEqual(pipeline.project_version.findings.count(), 1)

        self.assertEqual(pipeline.launch_data.get("source"), "manual_import")
        self.assertEqual(pipeline.launch_data.get("scan_type"), DAST_SCAN_TYPE)
        self.assertEqual(pipeline.launch_data.get("uploader_id"), self.uploader.id)
        self.assertEqual(pipeline.launch_data.get("filename"), "generic-aist-report.json")
        self.assertIn("dast_finalization", pipeline.launch_data)

        test_obj = pipeline.tests.first()
        self.assertEqual(test_obj.test_type.name, "DAST Autonomous Scan")
        self.assertFalse(default_storage.exists(self.storage_name))

    def test_duplicate_delivery_after_completed_import_is_idempotent(self):
        import_report.run(
            pipeline_id=self.pipeline.id,
            storage_name=self.storage_name,
            project_id=self.project.id,
            uploader_id=self.uploader.id,
            scan_type=DAST_SCAN_TYPE,
            commit_hash=SHA,
            filename="generic-aist-report.json",
            sha256=self.report_sha256,
            binding_id=self.binding.id,
        )
        first_test_id = self.pipeline.tests.values_list("id", flat=True).get()
        duplicate_storage = default_storage.save(
            "report_imports/importtest1-duplicate.json",
            ContentFile(self.report_bytes),
        )

        import_report.run(
            pipeline_id=self.pipeline.id,
            storage_name=duplicate_storage,
            project_id=self.project.id,
            uploader_id=self.uploader.id,
            scan_type=DAST_SCAN_TYPE,
            commit_hash="",
            filename="generic-aist-report.json",
            sha256=self.report_sha256,
            binding_id=self.binding.id,
        )

        self.assertFalse(default_storage.exists(duplicate_storage))
        self.assertEqual(self.pipeline.tests.count(), 1)
        self.assertEqual(self.pipeline.tests.values_list("id", flat=True).get(), first_test_id)
