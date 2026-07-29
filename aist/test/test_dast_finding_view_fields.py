"""
Tests that the finding API surfaces the fields the DAST client-ui finding view
needs (affected_endpoints, dynamic_finding, steps_to_reproduce, param, payload,
cvssv3) for a finding imported from a manual DAST report.
"""
from __future__ import annotations

import hashlib
import json

from django.contrib.auth import get_user_model
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.test import Client, TestCase
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
                "steps_to_reproduce": "1. Authenticate as tenant A\n2. Request tenant B's resource",
                "param": "subscription_id",
                "payload": "' OR 1=1--",
                "cvssv3": "CVSS:3.1/AV:N/AC:L/PR:L/UI:N/S:U/C:H/I:N/A:N",
                "cvssv3_score": 6.5,
                "dynamic_finding": True,
                "references": "CWE-639\nhttps://dast-triage.internal/cp-backend/x.html",
                "unique_id_from_tool": "BOLA-cross-cp-cross-tenant-bola",
                "vuln_id_from_tool": "BOLA-cross-cp-cross-tenant-bola",
                "endpoints": ["https://api.example.com/v1/subscriptions/123"],
            },
        ],
        "dast_run_metadata": {
            "run_id": "run-dastview1",
            "target": "cloud-app",
            "stand": "qa-1",
            "source_commits": {"backend": SHA},
        },
    }


def _terminal_payload() -> dict:
    return {
        "contract_version": "2.0",
        "run_id": "run-dastview1",
        "status": "succeeded",
        "selection": {"stand_id": "qa-1", "relation": "exact", "distance": 0},
        "trigger_resolution": {
            "type": "GIT_HASH",
            "ref": SHA,
            "resolved_commit": SHA,
            "resolved_at": "2026-07-26T10:00:00Z",
        },
        "dast_run_metadata": {"source_commits": {"backend": SHA}},
        "report": _report_payload(),
        "audit": {"correlation_id": "manual-dastview1", "source_verified": True},
    }


class DastFindingViewFieldsTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username="dast-finding-view-user", password="pass")  # noqa: S106
        prod_type = Product_Type.objects.create(name="Dast Finding View PT")
        role_maintainer, _ = Role.objects.get_or_create(id=Roles.Maintainer, defaults={"name": "Maintainer"})
        Product_Type_Member.objects.create(product_type=prod_type, user=self.user, role=role_maintainer)

        organization = Organization.objects.create(name="Dast Finding View Org", product_type=prod_type)
        product = Product.objects.create(
            name="Dast Finding View Product",
            description="desc",
            prod_type=prod_type,
            sla_configuration=SLA_Configuration.objects.create(name="Dast Finding View SLA"),
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
            name="Dast Finding View Integration",
            is_active=True,
        )
        target = DastTarget.objects.create(
            integration=integration,
            provider_id="cloud-app",
            display_name="Cloud app",
            contract_revision="2.0",
            capability_revision="sha256:dast-view-capability",
            schema_digest="sha256:dast-view-schema",
            parameter_schema={"type": "object", "additionalProperties": False},
            provider_defaults={},
            repository_keys=["backend"],
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
            id="dastview1",
            project=self.project,
            execution_type=PipelineExecutionType.MANUAL_IMPORT,
            status=AISTStatus.ADMITTED,
            launch_data={"source": "manual_import", "dast_binding_id": binding.pk},
        )
        report_bytes = json.dumps(_terminal_payload()).encode()
        storage_name = default_storage.save(
            "report_imports/dastview1.json",
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

        self.client = Client()
        self.client.force_login(self.user)

    def test_finding_detail_exposes_dast_fields(self):
        response = self.client.get(reverse("aist_api:finding_detail", kwargs={"finding_id": self.finding.id}))
        self.assertEqual(response.status_code, 200, response.content)
        data = response.json()

        self.assertTrue(data["dynamic_finding"])
        self.assertEqual(data["param"], "subscription_id")
        self.assertEqual(data["payload"], "' OR 1=1--")
        self.assertIn("Authenticate as tenant A", data["steps_to_reproduce"])
        self.assertEqual(data["cvssv3_score"], 6.5)
        self.assertEqual(data["affected_endpoints"], ["https://api.example.com/v1/subscriptions/123"])
        self.assertEqual(data["references"], "CWE-639\nhttps://dast-triage.internal/cp-backend/x.html")
