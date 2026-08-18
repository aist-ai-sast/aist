"""
Guard test: a finding imported from a manual DAST report is an ordinary
dojo.Finding row, so Notes, History (timeline), and Work Items must work on it
exactly like on a SAST finding. No DAST-specific code path exists for any of
these three features — this test exists to catch a future regression if one
ever creeps in (e.g. a DAST-specific field breaking an assumption such as a
null filePath).
"""
from __future__ import annotations

import hashlib
import json
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from dojo.authorization.roles_permissions import Roles
from dojo.models import Finding, Product, Product_Type, Product_Type_Member, Role, SLA_Configuration

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
                "dynamic_finding": True,
                "endpoints": ["https://api.example.com/v1/subscriptions/123"],
            },
        ],
        "dast_run_metadata": {
            "run_id": "run-dastguard1",
            "target": "cloud-app",
            "stand": "qa-1",
            "source_commits": {"backend": SHA},
        },
    }


def _uploaded_payload() -> dict:
    """What an operator uploads: the exported report itself, no transport wrapper."""
    return _report_payload()


class DastFindingRegressionGuardTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username="dast-guard-user", password="pass")  # noqa: S106
        prod_type = Product_Type.objects.create(name="Dast Guard PT")
        role_maintainer, _ = Role.objects.get_or_create(id=Roles.Maintainer, defaults={"name": "Maintainer"})
        Product_Type_Member.objects.create(product_type=prod_type, user=self.user, role=role_maintainer)

        organization = Organization.objects.create(name="Dast Guard Org", product_type=prod_type)
        product = Product.objects.create(
            name="Dast Guard Product",
            description="desc",
            prod_type=prod_type,
            sla_configuration=SLA_Configuration.objects.create(name="Dast Guard SLA"),
        )
        repository = RepositoryInfo.objects.create(type=ScmType.GITHUB, repo_owner="acme", repo_name="cloud_portal")
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
            name="Dast Guard Integration",
            is_active=True,
        )
        target = DastTarget.objects.create(
            integration=integration,
            provider_id="cloud-app",
            display_name="Cloud app",
            contract_revision="2.0",
            capability_revision="sha256:dast-guard-capability",
            schema_digest="sha256:dast-guard-schema",
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
        binding = DastProjectBinding.objects.create(
            project=self.project,
            target=target,
            source_repo_key="backend",
            enabled=True,
        )
        self.pipeline = AISTPipeline.objects.create(
            id="dastguard1",
            project=self.project,
            execution_type=PipelineExecutionType.MANUAL_IMPORT,
            status=AISTStatus.ADMITTED,
            launch_data={"source": "manual_import", "dast_binding_id": binding.pk},
        )
        report_bytes = json.dumps(_uploaded_payload()).encode()
        storage_name = default_storage.save(
            "report_imports/dastguard1.json",
            ContentFile(report_bytes),
        )
        import_report(
            pipeline_id=self.pipeline.id,
            storage_name=storage_name,
            project_id=self.project.id,
            uploader_id=self.user.id,
            scan_type=DAST_SCAN_TYPE,
            commit_hash="",
            filename="generic-aist-report.json",
            sha256=hashlib.sha256(report_bytes).hexdigest(),
            binding_id=binding.pk,
        )
        self.finding = Finding.objects.get(test__aist_pipelines=self.pipeline)

        self.client.force_login(self.user)

    def test_can_add_a_note_to_an_imported_finding(self):
        response = self.client.post(
            reverse("aist_api:finding_notes", kwargs={"finding_id": self.finding.id}),
            data={"entry": "Confirmed exploitable in staging."},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 201, response.content)

        list_response = self.client.get(
            reverse("aist_api:finding_notes", kwargs={"finding_id": self.finding.id}),
        )
        self.assertEqual(list_response.status_code, 200)
        entries = [note["entry"] for note in list_response.json()]
        self.assertIn("Confirmed exploitable in staging.", entries)

    def test_imported_finding_appears_in_the_timeline(self):
        start = (timezone.now() - timedelta(days=1)).isoformat()
        end = (timezone.now() + timedelta(days=1)).isoformat()
        response = self.client.get(
            reverse("aist_api:finding_timeline"),
            data={"start": start, "end": end, "finding_id": self.finding.id},
        )
        self.assertEqual(response.status_code, 200, response.content)
        events = response.json().get("events", [])
        self.assertTrue(any(event["finding_id"] == self.finding.id for event in events))

    def test_can_link_a_work_item_to_an_imported_finding(self):
        response = self.client.post(
            reverse("aist_api:finding_work_item_list_create", kwargs={"finding_id": self.finding.id}),
            data={
                "external_url": "https://jira.example.com/browse/SEC-123",
                "external_key": "SEC-123",
                "title": "Fix cross-tenant BOLA",
            },
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 201, response.content)

        list_response = self.client.get(
            reverse("aist_api:finding_work_item_list_create", kwargs={"finding_id": self.finding.id}),
        )
        self.assertEqual(list_response.status_code, 200)
        keys = [link["external_key"] for link in list_response.json()]
        self.assertIn("SEC-123", keys)
