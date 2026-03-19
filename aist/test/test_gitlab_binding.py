from __future__ import annotations

from unittest.mock import Mock, patch

import gitlab
from django.test import TestCase
from dojo.models import Product_Type

from aist.models import Organization, OrgIntegration, RepositoryInfo, ScmGitlabBinding, ScmType


class GitlabBindingTests(TestCase):
    def setUp(self):
        self.repo = RepositoryInfo.objects.create(
            type=ScmType.GITLAB,
            repo_owner="group",
            repo_name="repo",
            base_url="https://gitlab.example.com",
        )
        org = Organization.objects.create(
            name="Binding Test Org",
            product_type=Product_Type.objects.create(name="Binding PT"),
        )
        self.integration = OrgIntegration.objects.create(
            organization=org,
            integration_type="GITLAB",
            name="Test GitLab",
            secret="token",  # noqa: S106
        )
        self.binding = ScmGitlabBinding.objects.create(scm=self.repo, org_integration=self.integration)

    @patch("aist.models.gitlab.Gitlab")
    def test_get_project_info_returns_attributes(self, mock_gitlab):
        mock_project = Mock()
        mock_project.attributes = {"default_branch": "main"}
        mock_gitlab.return_value.projects.get.return_value = mock_project

        info = self.binding.get_project_info(self.repo)

        self.assertEqual(info, {"default_branch": "main"})

    @patch("aist.models.gitlab.Gitlab")
    def test_get_project_info_handles_not_found(self, mock_gitlab):
        mock_gitlab.return_value.projects.get.side_effect = gitlab.exceptions.GitlabGetError(
            error_message="Not Found",
            response_code=404,
            response_body="",
        )

        info = self.binding.get_project_info(self.repo)

        self.assertIsNone(info)
