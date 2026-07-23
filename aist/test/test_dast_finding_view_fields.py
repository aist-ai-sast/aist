"""
Tests that the finding API surfaces the fields the DAST client-ui finding view
needs (affected_endpoints, dynamic_finding, steps_to_reproduce, param, payload,
cvssv3) for a finding imported from a manual DAST report.
"""
from __future__ import annotations

import json

from django.contrib.auth import get_user_model
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.test import Client, TestCase
from django.urls import reverse
from dojo.authorization.roles_permissions import Roles
from dojo.models import Finding, Product, Product_Type, Product_Type_Member, Role

from aist.models import AISTPipeline, AISTProject, AISTStatus, RepositoryInfo, ScmType
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
    }


class DastFindingViewFieldsTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username="dast-finding-view-user", password="pass")  # noqa: S106
        prod_type = Product_Type.objects.create(name="Dast Finding View PT")
        role_maintainer, _ = Role.objects.get_or_create(id=Roles.Maintainer, defaults={"name": "Maintainer"})
        Product_Type_Member.objects.create(product_type=prod_type, user=self.user, role=role_maintainer)

        product = Product.objects.create(name="Dast Finding View Product", description="desc", prod_type=prod_type)
        repository = RepositoryInfo.objects.create(type=ScmType.GITHUB, repo_owner="acme", repo_name="cloud_portal")
        self.project = AISTProject.objects.create(
            product=product,
            supported_languages=[],
            compilable=False,
            profile={},
            repository=repository,
        )
        self.pipeline = AISTPipeline.objects.create(
            id="dastview1",
            project=self.project,
            status=AISTStatus.FINISHED,
        )
        storage_name = default_storage.save(
            "report_imports/dastview1.json",
            ContentFile(json.dumps(_report_payload()).encode()),
        )
        import_report(
            pipeline_id=self.pipeline.id,
            storage_name=storage_name,
            project_id=self.project.id,
            uploader_id=self.user.id,
            scan_type=DAST_SCAN_TYPE,
            commit_hash=SHA,
            filename="generic-aist-report.json",
            sha256="deadbeef",
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
