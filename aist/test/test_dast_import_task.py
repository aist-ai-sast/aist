"""
Integration tests for the generic manual-report-import Celery task, exercised end-to-end
with a DAST report (the one format currently wired into client-ui). The task itself is
format-blind: it takes ``scan_type``/``commit_hash`` as plain parameters, so these tests
also cover the full real import path through ``DastReportParser``
(``aist/parser_overrides.py``).
"""
from __future__ import annotations

import json

from django.contrib.auth import get_user_model
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.test import TestCase
from dojo.models import Product, Product_Type

from aist.models import (
    AISTPipeline,
    AISTProject,
    AISTStatus,
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
        "version": "v1",
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
    }


class ImportReportTaskTests(TestCase):
    def setUp(self):
        prod_type = Product_Type.objects.create(name="Report Import Task PT")
        product = Product.objects.create(name="Report Import Task Product", description="desc", prod_type=prod_type)
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
        self.uploader = get_user_model().objects.create_user(username="report-importer")
        self.pipeline = AISTPipeline.objects.create(
            id="importtest1",
            project=self.project,
            status=AISTStatus.FINISHED,
        )
        self.storage_name = default_storage.save(
            "report_imports/importtest1.json",
            ContentFile(json.dumps(_report_payload()).encode()),
        )

    def test_import_attaches_findings_and_hands_off_to_deduplication(self):
        # Mirrors run_sast_pipeline: when there are findings, the task defers to
        # postprocess_findings, which leaves the pipeline in
        # WAITING_DEDUPLICATION_TO_FINISH until the (separately scheduled)
        # watch_deduplication task later calls finish_pipeline. That handoff runs
        # via transaction.on_commit, which TestCase's per-test transaction never
        # fires — so the terminal FINISHED state is out of scope here and is
        # covered by the no-findings path below instead.
        import_report(
            pipeline_id=self.pipeline.id,
            storage_name=self.storage_name,
            project_id=self.project.id,
            uploader_id=self.uploader.id,
            scan_type=DAST_SCAN_TYPE,
            commit_hash=SHA,
            filename="generic-aist-report.json",
            sha256="deadbeef",
        )

        pipeline = AISTPipeline.objects.get(id=self.pipeline.id)
        self.assertEqual(pipeline.status, AISTStatus.WAITING_DEDUPLICATION_TO_FINISH)
        self.assertIsNotNone(pipeline.project_version)
        self.assertEqual(pipeline.project_version.version, SHA)
        self.assertEqual(pipeline.project_version.version_type, VersionType.GIT_HASH)
        self.assertGreaterEqual(pipeline.tests.count(), 1)
        self.assertGreaterEqual(pipeline.project_version.findings.count(), 1)

        self.assertEqual(pipeline.launch_data.get("source"), "manual_import")
        self.assertEqual(pipeline.launch_data.get("scan_type"), DAST_SCAN_TYPE)
        self.assertEqual(pipeline.launch_data.get("uploader_id"), self.uploader.id)
        self.assertEqual(pipeline.launch_data.get("filename"), "generic-aist-report.json")
        self.assertEqual(pipeline.launch_data.get("sha256"), "deadbeef")

        test_obj = pipeline.tests.first()
        self.assertEqual(test_obj.test_type.name, "DAST Autonomous Scan")

    def test_temp_file_is_deleted_after_import(self):
        import_report(
            pipeline_id=self.pipeline.id,
            storage_name=self.storage_name,
            project_id=self.project.id,
            uploader_id=self.uploader.id,
            scan_type=DAST_SCAN_TYPE,
            commit_hash=SHA,
            filename="generic-aist-report.json",
            sha256="deadbeef",
        )
        self.assertFalse(default_storage.exists(self.storage_name))

    def test_duplicate_delivery_during_active_import_preserves_owner_upload(self):
        self.pipeline.status = AISTStatus.UPLOADING_RESULTS
        self.pipeline.launch_data = {"source": "manual_import", "sha256": "deadbeef"}
        self.pipeline.save(update_fields=["status", "launch_data"])
        self.addCleanup(default_storage.delete, self.storage_name)

        import_report(
            pipeline_id=self.pipeline.id,
            storage_name=self.storage_name,
            project_id=self.project.id,
            uploader_id=self.uploader.id,
            scan_type=DAST_SCAN_TYPE,
            commit_hash=SHA,
            filename="generic-aist-report.json",
            sha256="deadbeef",
        )

        self.assertTrue(default_storage.exists(self.storage_name))
        self.assertEqual(self.pipeline.tests.count(), 0)

    def test_duplicate_delivery_after_completed_import_is_idempotent(self):
        self.pipeline.launch_data = {"source": "manual_import", "sha256": "deadbeef"}
        self.pipeline.save(update_fields=["launch_data"])

        import_report(
            pipeline_id=self.pipeline.id,
            storage_name=self.storage_name,
            project_id=self.project.id,
            uploader_id=self.uploader.id,
            scan_type=DAST_SCAN_TYPE,
            commit_hash=SHA,
            filename="generic-aist-report.json",
            sha256="deadbeef",
        )

        self.assertFalse(default_storage.exists(self.storage_name))
        self.assertEqual(self.pipeline.tests.count(), 0)
