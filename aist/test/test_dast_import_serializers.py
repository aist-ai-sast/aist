"""
Unit tests for generic report-import request validation: authorized project relations,
registered scan types, and upload size.
"""
from __future__ import annotations

from types import SimpleNamespace

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.utils import timezone
from dojo.authorization.roles_permissions import Roles
from dojo.models import Product, Product_Type, Product_Type_Member, Role, SLA_Configuration

from aist.api.report_import import PipelineImportRequestSerializer, PipelineImportValidateRequestSerializer
from aist.models import (
    AISTProject,
    DastProjectBinding,
    DastTarget,
    Organization,
    OrgIntegration,
    OrgIntegrationType,
    RepositoryInfo,
    ScmType,
)
from aist.parser_overrides import DAST_SCAN_TYPE


def _upload(content: bytes = b"{}", name: str = "report.json") -> SimpleUploadedFile:
    return SimpleUploadedFile(name, content, content_type="application/json")


class ReportImportSerializerTestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = get_user_model().objects.create_user(username=f"{cls.__name__}-user")
        sla = SLA_Configuration.objects.create(name=f"{cls.__name__} SLA")
        product_type = Product_Type.objects.create(name=f"{cls.__name__} PT")
        organization = Organization.objects.create(name=f"{cls.__name__} Org", product_type=product_type)
        maintainer, _ = Role.objects.get_or_create(id=Roles.Maintainer, defaults={"name": "Maintainer"})
        Product_Type_Member.objects.create(product_type=product_type, user=cls.user, role=maintainer)
        product = Product.objects.create(
            name=f"{cls.__name__} Product",
            description="desc",
            prod_type=product_type,
            sla_configuration=sla,
        )
        cls.project = AISTProject.objects.create(
            product=product,
            supported_languages=[],
            compilable=False,
            profile={},
            repository=RepositoryInfo.objects.create(
                type=ScmType.GITHUB,
                repo_owner="acme",
                repo_name=f"{cls.__name__.lower()}-repo",
            ),
        )
        integration = OrgIntegration.objects.create(
            organization=organization,
            integration_type=OrgIntegrationType.DAST,
            name=f"{cls.__name__} DAST",
            is_active=True,
        )
        target = DastTarget.objects.create(
            integration=integration,
            provider_id="cloud-app",
            display_name="Cloud app",
            contract_revision="2.0",
            capability_revision="sha256:serializer-capability",
            schema_digest="sha256:serializer-schema",
            parameter_schema={"type": "object", "additionalProperties": False},
            provider_defaults={},
            repository_keys=["backend"],
            autonomous_ready=True,
            last_seen_at=timezone.now(),
        )
        cls.binding = DastProjectBinding.objects.create(
            project=cls.project,
            target=target,
            source_repo_key="backend",
            enabled=True,
        )
        other_product_type = Product_Type.objects.create(name=f"{cls.__name__} Other PT")
        other_product = Product.objects.create(
            name=f"{cls.__name__} Other Product",
            description="desc",
            prod_type=other_product_type,
            sla_configuration=sla,
        )
        cls.other_project = AISTProject.objects.create(
            product=other_product,
            supported_languages=[],
            compilable=False,
            profile={},
        )

    def serializer_context(self) -> dict:
        return {"request": SimpleNamespace(user=self.user)}


class PipelineImportValidateRequestSerializerTests(ReportImportSerializerTestCase):
    def test_registered_scan_type_is_accepted(self):
        serializer = PipelineImportValidateRequestSerializer(
            data={
                "file": _upload(),
                "project_id": self.project.id,
                "scan_type": DAST_SCAN_TYPE,
                "binding_id": self.binding.id,
            },
            context=self.serializer_context(),
        )
        self.assertTrue(serializer.is_valid(), serializer.errors)
        self.assertEqual(serializer.validated_data["project"], self.project)

    def test_unknown_project_is_rejected_by_serializer(self):
        serializer = PipelineImportValidateRequestSerializer(
            data={
                "file": _upload(),
                "project_id": self.project.id + 9999,
                "scan_type": DAST_SCAN_TYPE,
                "binding_id": self.binding.id,
            },
            context=self.serializer_context(),
        )
        self.assertFalse(serializer.is_valid())
        self.assertIn("project_id", serializer.errors)

    def test_project_outside_user_scope_is_rejected_by_serializer(self):
        serializer = PipelineImportValidateRequestSerializer(
            data={"file": _upload(), "project_id": self.other_project.id, "scan_type": DAST_SCAN_TYPE},
            context=self.serializer_context(),
        )
        self.assertFalse(serializer.is_valid())
        self.assertIn("project_id", serializer.errors)

    def test_unregistered_scan_type_is_rejected(self):
        serializer = PipelineImportValidateRequestSerializer(
            data={"file": _upload(), "project_id": self.project.id, "scan_type": "Not A Real Parser"},
            context=self.serializer_context(),
        )
        self.assertFalse(serializer.is_valid())
        self.assertIn("scan_type", serializer.errors)

    @override_settings(PIPELINE_IMPORT_MAX_SIZE_BYTES=10)
    def test_oversized_file_is_rejected(self):
        serializer = PipelineImportValidateRequestSerializer(
            data={"file": _upload(b"x" * 100), "project_id": self.project.id, "scan_type": DAST_SCAN_TYPE},
            context=self.serializer_context(),
        )
        self.assertFalse(serializer.is_valid())
        self.assertIn("file", serializer.errors)


class PipelineImportRequestSerializerTests(ReportImportSerializerTestCase):
    def test_dast_requires_binding_and_does_not_accept_commit_override(self):
        serializer = PipelineImportRequestSerializer(
            data={
                "file": _upload(),
                "project_id": self.project.id,
                "scan_type": DAST_SCAN_TYPE,
                "binding_id": self.binding.id,
            },
            context=self.serializer_context(),
        )
        self.assertTrue(serializer.is_valid(), serializer.errors)
        self.assertEqual(serializer.validated_data["project"], self.project)

        override = PipelineImportRequestSerializer(
            data={
                "file": _upload(),
                "project_id": self.project.id,
                "scan_type": DAST_SCAN_TYPE,
                "binding_id": self.binding.id,
                "commit_hash": "fd5b25aa1234567890abcdef1234567890abcdef",
            },
            context=self.serializer_context(),
        )
        self.assertFalse(override.is_valid())
        self.assertIn("commit_hash", override.errors)

    def test_disabled_dast_binding_is_rejected(self):
        self.binding.enabled = False
        self.binding.save(update_fields=["enabled"])
        serializer = PipelineImportRequestSerializer(
            data={
                "file": _upload(),
                "project_id": self.project.id,
                "scan_type": DAST_SCAN_TYPE,
                "binding_id": self.binding.id,
            },
            context=self.serializer_context(),
        )

        self.assertFalse(serializer.is_valid())
        self.assertIn("binding_id", serializer.errors)

    def test_unknown_project_is_rejected_by_serializer(self):
        serializer = PipelineImportRequestSerializer(
            data={
                "file": _upload(),
                "project_id": self.project.id + 9999,
                "scan_type": DAST_SCAN_TYPE,
                "binding_id": self.binding.id,
            },
            context=self.serializer_context(),
        )
        self.assertFalse(serializer.is_valid())
        self.assertIn("project_id", serializer.errors)

    def test_project_outside_user_scope_is_rejected_by_serializer(self):
        serializer = PipelineImportRequestSerializer(
            data={
                "file": _upload(),
                "project_id": self.other_project.id,
                "scan_type": DAST_SCAN_TYPE,
                "commit_hash": "deadbeef",
            },
            context=self.serializer_context(),
        )
        self.assertFalse(serializer.is_valid())
        self.assertIn("project_id", serializer.errors)

    def test_missing_dast_binding_is_rejected(self):
        serializer = PipelineImportRequestSerializer(
            data={"file": _upload(), "project_id": self.project.id, "scan_type": DAST_SCAN_TYPE},
            context=self.serializer_context(),
        )
        self.assertFalse(serializer.is_valid())
        self.assertIn("binding_id", serializer.errors)

    def test_unregistered_scan_type_is_rejected(self):
        serializer = PipelineImportRequestSerializer(
            data={
                "file": _upload(),
                "project_id": self.project.id,
                "scan_type": "Not A Real Parser",
                "commit_hash": "deadbeef",
            },
            context=self.serializer_context(),
        )
        self.assertFalse(serializer.is_valid())
        self.assertIn("scan_type", serializer.errors)

    @override_settings(PIPELINE_IMPORT_MAX_SIZE_BYTES=10)
    def test_oversized_file_is_rejected(self):
        serializer = PipelineImportRequestSerializer(
            data={
                "file": _upload(b"x" * 100),
                "project_id": self.project.id,
                "scan_type": DAST_SCAN_TYPE,
                "binding_id": self.binding.id,
            },
            context=self.serializer_context(),
        )
        self.assertFalse(serializer.is_valid())
        self.assertIn("file", serializer.errors)
