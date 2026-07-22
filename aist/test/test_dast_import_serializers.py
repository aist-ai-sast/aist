"""
Unit tests for generic report-import request validation: authorized project relations,
registered scan types, and upload size.
"""
from __future__ import annotations

from types import SimpleNamespace

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from dojo.authorization.roles_permissions import Roles
from dojo.models import Product, Product_Type, Product_Type_Member, Role

from aist.api.report_import import PipelineImportRequestSerializer, PipelineImportValidateRequestSerializer
from aist.models import AISTProject
from aist.parser_overrides import DAST_SCAN_TYPE


def _upload(content: bytes = b"{}", name: str = "report.json") -> SimpleUploadedFile:
    return SimpleUploadedFile(name, content, content_type="application/json")


class ReportImportSerializerTestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = get_user_model().objects.create_user(username=f"{cls.__name__}-user")
        product_type = Product_Type.objects.create(name=f"{cls.__name__} PT")
        maintainer, _ = Role.objects.get_or_create(id=Roles.Maintainer, defaults={"name": "Maintainer"})
        Product_Type_Member.objects.create(product_type=product_type, user=cls.user, role=maintainer)
        product = Product.objects.create(name=f"{cls.__name__} Product", description="desc", prod_type=product_type)
        cls.project = AISTProject.objects.create(
            product=product,
            supported_languages=[],
            compilable=False,
            profile={},
        )
        other_product_type = Product_Type.objects.create(name=f"{cls.__name__} Other PT")
        other_product = Product.objects.create(
            name=f"{cls.__name__} Other Product",
            description="desc",
            prod_type=other_product_type,
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
            data={"file": _upload(), "project_id": self.project.id, "scan_type": DAST_SCAN_TYPE},
            context=self.serializer_context(),
        )
        self.assertTrue(serializer.is_valid(), serializer.errors)
        self.assertEqual(serializer.validated_data["project"], self.project)

    def test_unknown_project_is_rejected_by_serializer(self):
        serializer = PipelineImportValidateRequestSerializer(
            data={"file": _upload(), "project_id": self.project.id + 9999, "scan_type": DAST_SCAN_TYPE},
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
    def test_registered_scan_type_and_commit_hash_is_accepted(self):
        serializer = PipelineImportRequestSerializer(
            data={
                "file": _upload(),
                "project_id": self.project.id,
                "scan_type": DAST_SCAN_TYPE,
                "commit_hash": "fd5b25aa1234567890abcdef1234567890abcdef",
            },
            context=self.serializer_context(),
        )
        self.assertTrue(serializer.is_valid(), serializer.errors)
        self.assertEqual(serializer.validated_data["project"], self.project)

    def test_unknown_project_is_rejected_by_serializer(self):
        serializer = PipelineImportRequestSerializer(
            data={
                "file": _upload(),
                "project_id": self.project.id + 9999,
                "scan_type": DAST_SCAN_TYPE,
                "commit_hash": "deadbeef",
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

    def test_missing_commit_hash_is_rejected(self):
        serializer = PipelineImportRequestSerializer(
            data={"file": _upload(), "project_id": self.project.id, "scan_type": DAST_SCAN_TYPE},
            context=self.serializer_context(),
        )
        self.assertFalse(serializer.is_valid())
        self.assertIn("commit_hash", serializer.errors)

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
                "commit_hash": "deadbeef",
            },
            context=self.serializer_context(),
        )
        self.assertFalse(serializer.is_valid())
        self.assertIn("file", serializer.errors)
