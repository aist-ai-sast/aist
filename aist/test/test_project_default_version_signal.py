from __future__ import annotations

from unittest.mock import patch

from django.test import TestCase
from dojo.models import Product, Product_Type, SLA_Configuration

from aist.models import AISTProject, AISTProjectVersion, RepositoryInfo, ScmGithubBinding, ScmType


class ProjectDefaultVersionSignalTests(TestCase):
    def setUp(self):
        self.sla, _ = SLA_Configuration.objects.get_or_create(id=1, defaults={"name": "SLA default"})

    def test_project_create_falls_back_to_master_when_binding_info_unavailable(self):
        product_type = Product_Type.objects.create(name="PT-signal")
        product = Product.objects.create(
            name="Signal Product",
            description="desc",
            prod_type=product_type,
            sla_configuration_id=self.sla.id,
        )
        repo = RepositoryInfo.objects.create(
            type=ScmType.GITHUB,
            repo_owner="owner",
            repo_name="repo",
            base_url="https://github.com",
        )
        ScmGithubBinding.objects.create(scm=repo, installation_id=12345)

        with (
            patch("aist.models.ScmGithubBinding.get_project_info", return_value=None),
            self.captureOnCommitCallbacks(execute=True),
        ):
            project = AISTProject.objects.create(
                product=product,
                supported_languages=[],
                script_path="input_projects/default_imported_project_no_built.sh",
                compilable=False,
                profile={},
                repository=repo,
            )

        self.assertTrue(
            AISTProjectVersion.objects.filter(
                project=project,
                version="master",
            ).exists(),
        )
