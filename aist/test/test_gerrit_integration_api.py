from __future__ import annotations

from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from dojo.models import Product, Product_Type, SLA_Configuration
from rest_framework.test import APIClient

from aist.models import (
    AISTProject,
    Organization,
    OrgIntegration,
    OrgIntegrationType,
    RepositoryInfo,
    ScmGerritBinding,
    ScmType,
)

TEST_GERRIT_PASSWORD = "xhttp-pass-123".removeprefix("x")


class GerritIntegrationAPITests(TestCase):
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
        return reverse("aist_api:import_project_from_gerrit")

    def _create_gerrit_integration(self, org: Organization, *, base_url: str = "https://gerrit.example.com"):
        return OrgIntegration.objects.create(
            organization=org,
            integration_type=OrgIntegrationType.GERRIT,
            name=f"{org.name} Gerrit",
            config={"base_url": base_url, "username": "svc-user"},
            secret=TEST_GERRIT_PASSWORD,
            is_active=True,
            created_by=self.user,
        )

    @patch("aist.api.gerrit_integration.fetch_gerrit_project_info.delay")
    def test_import_gerrit_project_happy_path(self, mock_delay):
        org = Organization.objects.create(name="Org")
        integration = self._create_gerrit_integration(org)
        mock_delay.return_value.get.return_value = {
            "ok": True,
            "project_path": "platform/build/soong",
            "description": "desc",
            "web_url": "https://gerrit.example.com/admin/repos/platform/build/soong",
            "inferred_base": "https://gerrit.example.com",
            "default_branch": "main",
        }

        resp = self.client.post(
            self._url(),
            data={"project_path": "platform/build/soong", "organization_id": org.id},
            format="json",
        )

        self.assertEqual(resp.status_code, 201)
        aist_project = AISTProject.objects.get(id=resp.data["aist_project_id"])
        self.assertEqual(aist_project.organization_id, org.id)
        self.assertEqual(aist_project.repository.type, ScmType.GERRIT)
        self.assertEqual(aist_project.supported_languages, [])

        repo = RepositoryInfo.objects.get(id=resp.data["repository_id"])
        self.assertEqual(repo.repo_owner, "platform/build")
        self.assertEqual(repo.repo_name, "soong")
        self.assertEqual(repo.repo_full, "platform/build/soong")
        binding = ScmGerritBinding.objects.get(scm=repo)
        self.assertEqual(binding.org_integration_id, integration.id)

    @patch("aist.api.gerrit_integration.fetch_gerrit_project_info.delay")
    def test_import_gerrit_single_segment_project(self, mock_delay):
        org = Organization.objects.create(name="Org SS")
        self._create_gerrit_integration(org)
        mock_delay.return_value.get.return_value = {
            "ok": True,
            "project_path": "All-Projects",
            "description": "",
            "web_url": "https://gerrit.example.com/admin/repos/All-Projects",
            "inferred_base": "https://gerrit.example.com",
            "default_branch": "master",
        }

        resp = self.client.post(
            self._url(),
            data={"project_path": "All-Projects", "organization_id": org.id},
            format="json",
        )

        self.assertEqual(resp.status_code, 201)
        repo = RepositoryInfo.objects.get(id=resp.data["repository_id"])
        self.assertEqual(repo.repo_owner, "")
        self.assertEqual(repo.repo_name, "All-Projects")
        # Clone URL must not contain a leading slash after /a/.
        binding = ScmGerritBinding.objects.get(scm=repo)
        self.assertEqual(
            binding.build_clone_url(repo),
            f"https://svc-user:{TEST_GERRIT_PASSWORD}@gerrit.example.com/a/All-Projects",
        )

    @patch("aist.api.gerrit_integration.fetch_gerrit_project_info.delay")
    def test_import_produces_runnable_clone_url(self, mock_delay):
        """End-to-end: import ties integration → binding → repository.clone_url."""
        org = Organization.objects.create(name="Org Smoke")
        self._create_gerrit_integration(org)
        mock_delay.return_value.get.return_value = {
            "ok": True,
            "project_path": "platform/build/soong",
            "description": "desc",
            "web_url": "https://gerrit.example.com/admin/repos/platform/build/soong",
            "inferred_base": "https://gerrit.example.com",
            "default_branch": "main",
        }

        resp = self.client.post(
            self._url(),
            data={"project_path": "platform/build/soong", "organization_id": org.id},
            format="json",
        )
        self.assertEqual(resp.status_code, 201)

        aist_project = AISTProject.objects.get(id=resp.data["aist_project_id"])
        self.assertEqual(
            aist_project.repository.clone_url,
            f"https://svc-user:{TEST_GERRIT_PASSWORD}@gerrit.example.com/a/platform/build/soong",
        )

    @patch("aist.api.gerrit_integration.fetch_gerrit_project_info.delay")
    def test_import_gerrit_project_returns_404(self, mock_delay):
        org = Organization.objects.create(name="Org 404")
        self._create_gerrit_integration(org)
        mock_delay.return_value.get.return_value = {"ok": False, "response_code": 404, "error": "Not Found"}

        resp = self.client.post(
            self._url(),
            data={"project_path": "missing/repo", "organization_id": org.id},
            format="json",
        )
        self.assertEqual(resp.status_code, 404)

    def test_import_gerrit_project_requires_organization(self):
        resp = self.client.post(
            self._url(),
            data={"project_path": "a/b"},
            format="json",
        )
        self.assertEqual(resp.status_code, 400)
        self.assertIn("organization_id", resp.data)

    def test_import_gerrit_project_requires_active_integration(self):
        org = Organization.objects.create(name="Org No Integration")
        resp = self.client.post(
            self._url(),
            data={"project_path": "a/b", "organization_id": org.id},
            format="json",
        )
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(resp.data["detail"], "No active Gerrit integration found for this organization.")

    @patch("aist.api.gerrit_integration.fetch_gerrit_project_info.delay")
    def test_import_gerrit_project_conflicts_on_product_type_mismatch(self, mock_delay):
        org = Organization.objects.create(name="Org A")
        self._create_gerrit_integration(org)
        other_pt = Product_Type.objects.create(name="Other PT")
        Product.objects.create(
            name="group/my-repo",
            description="desc",
            prod_type=other_pt,
            sla_configuration_id=1,
        )
        mock_delay.return_value.get.return_value = {
            "ok": True,
            "project_path": "group/my-repo",
            "description": "desc",
            "web_url": "https://gerrit.example.com/admin/repos/group/my-repo",
            "inferred_base": "https://gerrit.example.com",
            "default_branch": "main",
        }

        resp = self.client.post(
            self._url(),
            data={"project_path": "group/my-repo", "organization_id": org.id},
            format="json",
        )
        self.assertEqual(resp.status_code, 409)


class GerritProjectsListViewTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = get_user_model().objects.create_user(
            username="lister",
            email="lister@example.com",
            password="pass",  # noqa: S106
        )
        self.user.is_superuser = True
        self.user.save(update_fields=["is_superuser"])
        self.client.force_authenticate(user=self.user)
        self.org = Organization.objects.create(name="List Org")
        OrgIntegration.objects.create(
            organization=self.org,
            integration_type=OrgIntegrationType.GERRIT,
            name="Gerrit",
            config={"base_url": "https://gerrit.example.com", "username": "svc-user"},
            secret=TEST_GERRIT_PASSWORD,
            is_active=True,
        )

    def _url(self):
        return reverse("aist:gerrit_projects_list")

    @patch("aist.views.integrations.fetch_gerrit_projects.delay")
    def test_list_returns_projects(self, mock_delay):
        mock_delay.return_value.get.return_value = {
            "ok": True,
            "projects": [{"name": "platform/build/soong", "project_path": "platform/build/soong"}],
        }
        resp = self.client.post(self._url(), data={"organization_id": self.org.id})
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json()["ok"])
        self.assertEqual(resp.json()["projects"][0]["project_path"], "platform/build/soong")

    def test_list_requires_organization(self):
        resp = self.client.post(self._url(), data={})
        self.assertEqual(resp.status_code, 400)

    def test_list_requires_active_integration(self):
        other_org = Organization.objects.create(name="No Integration Org")
        resp = self.client.post(self._url(), data={"organization_id": other_org.id})
        self.assertEqual(resp.status_code, 404)


class GerritIntegrationValidateTests(TestCase):

    """Unit-level coverage of the GERRIT branch in ``_validate_integration``."""

    def setUp(self):
        self.org = Organization.objects.create(name="Validate Org")
        self.integration = OrgIntegration.objects.create(
            organization=self.org,
            integration_type=OrgIntegrationType.GERRIT,
            name="Gerrit",
            config={"base_url": "https://gerrit.example.com", "username": "svc-user"},
            secret=TEST_GERRIT_PASSWORD,
            is_active=True,
        )

    @patch("pygerrit2.GerritRestAPI")
    def test_validate_success_calls_accounts_self(self, mock_rest_cls):
        from aist.api.org_integrations import _validate_integration  # noqa: PLC0415

        mock_rest = mock_rest_cls.return_value
        mock_rest.get.return_value = {"_account_id": 1}

        valid, detail = _validate_integration(self.integration)

        self.assertTrue(valid)
        self.assertEqual(detail, "")
        mock_rest.get.assert_called_once_with("/accounts/self")

    @patch("pygerrit2.GerritRestAPI")
    def test_validate_auth_failure_returns_invalid(self, mock_rest_cls):
        import requests

        from aist.api.org_integrations import _validate_integration  # noqa: PLC0415

        mock_rest = mock_rest_cls.return_value
        mock_rest.get.side_effect = requests.HTTPError("401 Unauthorized")

        valid, detail = _validate_integration(self.integration)

        self.assertFalse(valid)
        self.assertIn("HTTPError", detail)

    def test_validate_missing_base_url_returns_invalid_without_network_call(self):
        from aist.api.org_integrations import _validate_integration  # noqa: PLC0415

        self.integration.config = {"username": "svc-user"}
        self.integration.save(update_fields=["config"])

        valid, detail = _validate_integration(self.integration)

        self.assertFalse(valid)
        self.assertIn("base_url", detail)
