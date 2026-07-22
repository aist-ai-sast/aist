"""Unit tests for GIT_HASH version resolution used by manual report import (any scan_type)."""
from __future__ import annotations

from django.test import TestCase
from dojo.models import Product, Product_Type

from aist.models import AISTProject, AISTProjectVersion, RepositoryInfo, ScmType, VersionType
from aist.utils.report_import import ReportImportError, resolve_import_version


class ResolveImportVersionTests(TestCase):
    def setUp(self):
        prod_type = Product_Type.objects.create(name="Report Import PT")
        product = Product.objects.create(name="Report Import Product", description="desc", prod_type=prod_type)
        repository = RepositoryInfo.objects.create(
            type=ScmType.GITHUB,
            repo_owner="acme",
            repo_name="cloud_portal",
        )
        self.project = AISTProject.objects.create(
            product=product,
            supported_languages=["python"],
            compilable=False,
            profile={},
            repository=repository,
        )

    def test_resolves_and_creates_version_from_commit_hash(self):
        version = resolve_import_version(self.project, "fd5b25aa1234567890abcdef1234567890abcdef")
        self.assertEqual(version.version, "fd5b25aa1234567890abcdef1234567890abcdef")
        self.assertEqual(version.version_type, VersionType.GIT_HASH)

    def test_reuses_existing_version_on_second_call(self):
        commit_hash = "fd5b25aa1234567890abcdef1234567890abcdef"
        first = resolve_import_version(self.project, commit_hash)
        second = resolve_import_version(self.project, commit_hash)
        self.assertEqual(first.id, second.id)
        self.assertEqual(
            AISTProjectVersion.objects.filter(project=self.project, version=first.version).count(),
            1,
        )

    def test_missing_commit_hash_raises(self):
        with self.assertRaises(ReportImportError):
            resolve_import_version(self.project, "")

    def test_project_without_repository_raises(self):
        prod_type = Product_Type.objects.create(name="No Repo PT")
        product = Product.objects.create(name="No Repo Product", description="desc", prod_type=prod_type)
        project = AISTProject.objects.create(
            product=product,
            supported_languages=["python"],
            compilable=False,
            profile={},
        )
        with self.assertRaises(ReportImportError):
            resolve_import_version(project, "deadbeef")
