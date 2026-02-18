from __future__ import annotations

from django.test import TestCase
from dojo.models import Product, Product_Type, SLA_Configuration

from aist.models import AISTProject, AISTProjectVersion, VersionType
from aist.utils.project_version_refs import resolve_project_version_git_refs


class ProjectVersionRefsTests(TestCase):
    def setUp(self):
        sla = SLA_Configuration.objects.create(name="SLA default")
        prod_type = Product_Type.objects.create(name="PT")
        product = Product.objects.create(
            name="Test Product",
            description="desc",
            prod_type=prod_type,
            sla_configuration_id=sla.id,
        )
        self.project = AISTProject.objects.create(
            product=product,
            supported_languages=["python"],
            script_path="scripts/build.sh",
            compilable=False,
            profile={},
        )

    def test_hash_version_resolves_commit_and_branch(self):
        branch = AISTProjectVersion.objects.create(
            project=self.project,
            version_type=VersionType.GIT_BRANCH,
            version="main",
        )
        hash_version = AISTProjectVersion.objects.create(
            project=self.project,
            version_type=VersionType.GIT_HASH,
            version="abc123",
            resolved_from_branch=branch,
        )

        refs = resolve_project_version_git_refs(hash_version)

        self.assertEqual(refs.branch, "main")
        self.assertEqual(refs.commit, "abc123")

    def test_branch_version_resolves_only_branch(self):
        branch = AISTProjectVersion.objects.create(
            project=self.project,
            version_type=VersionType.GIT_BRANCH,
            version="develop",
        )

        refs = resolve_project_version_git_refs(branch)

        self.assertEqual(refs.branch, "develop")
        self.assertIsNone(refs.commit)

    def test_file_hash_has_no_git_refs(self):
        file_hash = AISTProjectVersion.objects.create(
            project=self.project,
            version_type=VersionType.FILE_HASH,
            version="f" * 64,
        )

        refs = resolve_project_version_git_refs(file_hash)

        self.assertIsNone(refs.branch)
        self.assertIsNone(refs.commit)
