from __future__ import annotations

from unittest.mock import Mock, patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from dojo.models import Product, Product_Type, SLA_Configuration
from rest_framework.test import APIClient

from aist.models import (
    AISTProject,
    AISTProjectVersion,
    Organization,
    OrgIntegration,
    OrgIntegrationType,
    RepositoryInfo,
    ScmGitlabBinding,
    ScmType,
    VersionType,
)
from aist.utils.secrets import MASKED_VALUE

TEST_GITLAB_TOKEN = "xtoken".removeprefix("x")
TEST_GITLAB_PAT = "xglpat-abcdef12345678".removeprefix("x")


class GitlabIntegrationAPITests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = get_user_model().objects.create_user(
            username="tester",
            email="tester@example.com",
            password="pass",  # noqa: S106
        )
        self.user.is_superuser = True
        self.user.save(update_fields=["is_superuser"])
        self.client.force_authenticate(user=self.user)
        SLA_Configuration.objects.bulk_create(
            [SLA_Configuration(id=1, name="SLA default")],
            ignore_conflicts=True,
        )

    def _url(self):
        return reverse("aist_api:import_project_from_gitlab")

    def _create_gitlab_integration(
        self,
        org: Organization,
        *,
        secret: str | None = None,
        base_url: str = "https://gitlab.example.com",
    ):
        return OrgIntegration.objects.create(
            organization=org,
            integration_type=OrgIntegrationType.GITLAB,
            name=f"{org.name} GitLab",
            config={"base_url": base_url},
            secret=secret or TEST_GITLAB_TOKEN,
            is_active=True,
            created_by=self.user,
        )

    @patch("aist.api.gitlab_integration._load_analyzers_config")
    @patch("aist.api.gitlab_integration.fetch_gitlab_project_info.delay")
    def test_import_gitlab_project_happy_path(self, mock_delay, mock_cfg):
        org = Organization.objects.create(name="Org")
        integration = self._create_gitlab_integration(org)

        mock_cfg.return_value = Mock(convert_languages=Mock(return_value=["python"]))
        mock_delay.return_value.get.return_value = {
            "ok": True,
            "path_with_namespace": "group/my-repo",
            "description": "desc",
            "web_url": "https://gitlab.example.com/group/my-repo",
            "inferred_base": "https://gitlab.example.com",
            "langs_raw": {"Python": 80.0, "Go": 20.0},
        }

        resp = self.client.post(
            self._url(),
            data={
                "project_id": 123,
                "organization_id": org.id,
            },
            format="json",
        )

        self.assertEqual(resp.status_code, 201)
        self.assertIn("aist_project_id", resp.data)

        aist_project = AISTProject.objects.get(id=resp.data["aist_project_id"])
        self.assertEqual(aist_project.organization_id, org.id)
        self.assertEqual(aist_project.repository.type, ScmType.GITLAB)
        org.refresh_from_db()
        self.assertIsNotNone(org.product_type_id)
        self.assertEqual(aist_project.product.prod_type_id, org.product_type_id)

        repo = RepositoryInfo.objects.get(id=resp.data["repository_id"])
        binding = ScmGitlabBinding.objects.get(scm=repo)
        self.assertEqual(binding.org_integration_id, integration.id)
        self.assertEqual(binding.org_integration.secret, TEST_GITLAB_TOKEN)
        self.assertEqual(binding.org_integration.integration_type, "GITLAB")

    @patch("aist.api.gitlab_integration._load_analyzers_config")
    @patch("aist.api.gitlab_integration.fetch_gitlab_project_info.delay")
    def test_import_seeds_real_default_branch_not_hardcoded_master(self, mock_delay, mock_cfg):
        """Regression: initial version must use the real default branch, not a hardcoded "master"."""
        org = Organization.objects.create(name="Org DefaultBranch")
        self._create_gitlab_integration(org)
        mock_cfg.return_value = Mock(convert_languages=Mock(return_value=["python"]))
        mock_delay.return_value.get.return_value = {
            "ok": True,
            "path_with_namespace": "group/my-repo",
            "description": "desc",
            "web_url": "https://gitlab.example.com/group/my-repo",
            "inferred_base": "https://gitlab.example.com",
            "default_branch": "main",
            "langs_raw": {"Python": 80.0},
        }

        resp = self.client.post(
            self._url(),
            data={"project_id": 123, "organization_id": org.id},
            format="json",
        )

        self.assertEqual(resp.status_code, 201, resp.data)
        aist_project = AISTProject.objects.get(id=resp.data["aist_project_id"])
        versions = list(AISTProjectVersion.objects.filter(project=aist_project))
        self.assertEqual(len(versions), 1)
        self.assertEqual(versions[0].version, "main")
        self.assertEqual(versions[0].version_type, VersionType.GIT_BRANCH)

    @patch("aist.api.gitlab_integration.fetch_gitlab_project_info.delay")
    def test_import_gitlab_project_returns_404(self, mock_delay):
        org = Organization.objects.create(name="Org 404")
        self._create_gitlab_integration(org)
        mock_delay.return_value.get.return_value = {"ok": False, "response_code": 404, "error": "Not Found"}

        resp = self.client.post(
            self._url(),
            data={
                "project_id": 999,
                "organization_id": org.id,
            },
            format="json",
        )

        self.assertEqual(resp.status_code, 404)

    @patch("aist.api.gitlab_integration.fetch_gitlab_project_info.delay")
    def test_import_gitlab_project_masks_token_in_error_detail(self, mock_delay):
        org = Organization.objects.create(name="Org 502")
        self._create_gitlab_integration(org, secret=TEST_GITLAB_PAT)
        mock_delay.return_value.get.return_value = {
            "ok": False,
            "response_code": 500,
            "error": f"GitLab API error: {MASKED_VALUE}",
        }

        resp = self.client.post(
            self._url(),
            data={
                "project_id": 999,
                "organization_id": org.id,
            },
            format="json",
        )

        self.assertEqual(resp.status_code, 502)
        self.assertNotIn(TEST_GITLAB_PAT, resp.data.get("detail", ""))
        self.assertIn("GitLab API error:", resp.data.get("detail", ""))
        self.assertIn(MASKED_VALUE, resp.data.get("detail", ""))

    def test_import_gitlab_project_requires_organization(self):
        resp = self.client.post(
            self._url(),
            data={
                "project_id": 123,
            },
            format="json",
        )

        self.assertEqual(resp.status_code, 400)
        self.assertIn("organization_id", resp.data)

    def test_import_gitlab_project_requires_active_integration(self):
        org = Organization.objects.create(name="Org Missing Integration")
        resp = self.client.post(
            self._url(),
            data={
                "project_id": 123,
                "organization_id": org.id,
            },
            format="json",
        )

        self.assertEqual(resp.status_code, 404)
        self.assertEqual(resp.data["detail"], "No active GitLab integration found for this organization.")

    @patch("aist.api.gitlab_integration._load_analyzers_config")
    @patch("aist.api.gitlab_integration.fetch_gitlab_project_info.delay")
    def test_import_gitlab_project_conflicts_on_existing_product_type_mismatch(self, mock_delay, mock_cfg):
        org = Organization.objects.create(name="Org A")
        self._create_gitlab_integration(org)
        other_pt = Product_Type.objects.create(name="Other PT")
        Product.objects.create(
            name="group/my-repo",
            description="desc",
            prod_type=other_pt,
            sla_configuration_id=1,
        )

        mock_cfg.return_value = Mock(convert_languages=Mock(return_value=["python"]))
        mock_delay.return_value.get.return_value = {
            "ok": True,
            "path_with_namespace": "group/my-repo",
            "description": "desc",
            "web_url": "https://gitlab.example.com/group/my-repo",
            "inferred_base": "https://gitlab.example.com",
            "langs_raw": {"Python": 100.0},
        }

        resp = self.client.post(
            self._url(),
            data={
                "project_id": 123,
                "organization_id": org.id,
            },
            format="json",
        )

        self.assertEqual(resp.status_code, 409)
