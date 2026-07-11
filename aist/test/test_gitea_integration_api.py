from __future__ import annotations

from unittest.mock import MagicMock, patch

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
    ScmGiteaBinding,
    ScmType,
    VersionType,
)

TEST_GITEA_TOKEN = "xpat-token-abc123".removeprefix("x")


class GiteaIntegrationAPITests(TestCase):
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
        return reverse("aist_api:import_project_from_gitea")

    def _create_gitea_integration(self, org: Organization, *, base_url: str = "https://gitea.example.com"):
        return OrgIntegration.objects.create(
            organization=org,
            integration_type=OrgIntegrationType.GITEA,
            name=f"{org.name} Gitea",
            config={"base_url": base_url},
            secret=TEST_GITEA_TOKEN,
            is_active=True,
            created_by=self.user,
        )

    @patch("aist.api.gitea_integration.fetch_gitea_project_info.delay")
    def test_import_gitea_project_happy_path(self, mock_delay):
        org = Organization.objects.create(name="Org")
        integration = self._create_gitea_integration(org)
        mock_delay.return_value.get.return_value = {
            "ok": True,
            "path_with_namespace": "myorg/myrepo",
            "description": "desc",
            "web_url": "https://gitea.example.com/myorg/myrepo",
            "inferred_base": "https://gitea.example.com",
            "default_branch": "main",
            "langs_raw": {"Python": 1234},
        }

        resp = self.client.post(
            self._url(),
            data={"repo_full_name": "myorg/myrepo", "organization_id": org.id},
            format="json",
        )

        self.assertEqual(resp.status_code, 201, resp.data)
        aist_project = AISTProject.objects.get(id=resp.data["aist_project_id"])
        self.assertEqual(aist_project.organization_id, org.id)
        self.assertEqual(aist_project.repository.type, ScmType.GITEA)
        self.assertEqual(aist_project.supported_languages, ["python"])

        repo = RepositoryInfo.objects.get(id=resp.data["repository_id"])
        self.assertEqual(repo.repo_owner, "myorg")
        self.assertEqual(repo.repo_name, "myrepo")
        self.assertEqual(repo.repo_full, "myorg/myrepo")
        binding = ScmGiteaBinding.objects.get(scm=repo)
        self.assertEqual(binding.org_integration_id, integration.id)

    @patch("aist.api.gitea_integration.fetch_gitea_project_info.delay")
    def test_import_seeds_real_default_branch_not_hardcoded_master(self, mock_delay):
        """
        Regression: the initial AISTProjectVersion must use the repo's actual
        default branch fetched via the VPN-aware Celery task, not the
        hardcoded "master" fallback in create_default_master_version (which
        has no VPN/proxy awareness and silently falls back when the Gitea
        host is only reachable through a VPN sidecar).
        """
        org = Organization.objects.create(name="Org DefaultBranch")
        self._create_gitea_integration(org)
        mock_delay.return_value.get.return_value = {
            "ok": True,
            "path_with_namespace": "myorg/myrepo",
            "description": "desc",
            "web_url": "https://gitea.example.com/myorg/myrepo",
            "inferred_base": "https://gitea.example.com",
            "default_branch": "main",
            "langs_raw": {},
        }

        resp = self.client.post(
            self._url(),
            data={"repo_full_name": "myorg/myrepo", "organization_id": org.id},
            format="json",
        )

        self.assertEqual(resp.status_code, 201, resp.data)
        aist_project = AISTProject.objects.get(id=resp.data["aist_project_id"])
        versions = list(AISTProjectVersion.objects.filter(project=aist_project))
        self.assertEqual(len(versions), 1)
        self.assertEqual(versions[0].version, "main")
        self.assertEqual(versions[0].version_type, VersionType.GIT_BRANCH)

    @patch("aist.api.gitea_integration.fetch_gitea_project_info.delay")
    def test_import_produces_runnable_clone_url(self, mock_delay):
        """End-to-end: import ties integration → binding → repository.clone_url."""
        org = Organization.objects.create(name="Org Smoke")
        self._create_gitea_integration(org)
        mock_delay.return_value.get.return_value = {
            "ok": True,
            "path_with_namespace": "myorg/myrepo",
            "description": "desc",
            "web_url": "https://gitea.example.com/myorg/myrepo",
            "inferred_base": "https://gitea.example.com",
            "default_branch": "main",
            "langs_raw": {},
        }

        resp = self.client.post(
            self._url(),
            data={"repo_full_name": "myorg/myrepo", "organization_id": org.id},
            format="json",
        )
        self.assertEqual(resp.status_code, 201, resp.data)

        aist_project = AISTProject.objects.get(id=resp.data["aist_project_id"])
        self.assertEqual(
            aist_project.repository.clone_url,
            f"https://{TEST_GITEA_TOKEN}@gitea.example.com/myorg/myrepo.git",
        )

    @patch("aist.api.gitea_integration.fetch_gitea_project_info.delay")
    def test_import_gitea_project_returns_404(self, mock_delay):
        org = Organization.objects.create(name="Org 404")
        self._create_gitea_integration(org)
        mock_delay.return_value.get.return_value = {"ok": False, "response_code": 404, "error": "Not Found"}

        resp = self.client.post(
            self._url(),
            data={"repo_full_name": "missing/repo", "organization_id": org.id},
            format="json",
        )
        self.assertEqual(resp.status_code, 404)

    def test_import_gitea_project_requires_organization(self):
        resp = self.client.post(
            self._url(),
            data={"repo_full_name": "a/b"},
            format="json",
        )
        self.assertEqual(resp.status_code, 400)
        self.assertIn("organization_id", resp.data)

    def test_import_gitea_project_rejects_name_without_slash(self):
        org = Organization.objects.create(name="Org Bad Name")
        self._create_gitea_integration(org)
        resp = self.client.post(
            self._url(),
            data={"repo_full_name": "no-slash-here", "organization_id": org.id},
            format="json",
        )
        self.assertEqual(resp.status_code, 400)

    def test_import_gitea_project_requires_active_integration(self):
        org = Organization.objects.create(name="Org No Integration")
        resp = self.client.post(
            self._url(),
            data={"repo_full_name": "a/b", "organization_id": org.id},
            format="json",
        )
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(resp.data["detail"], "No active Gitea integration found for this organization.")

    @patch("aist.api.gitea_integration.fetch_gitea_project_info.delay")
    def test_import_gitea_project_conflicts_on_product_type_mismatch(self, mock_delay):
        org = Organization.objects.create(name="Org A")
        self._create_gitea_integration(org)
        other_pt = Product_Type.objects.create(name="Other PT")
        Product.objects.create(
            name="myorg/myrepo",
            description="desc",
            prod_type=other_pt,
            sla_configuration_id=1,
        )
        mock_delay.return_value.get.return_value = {
            "ok": True,
            "path_with_namespace": "myorg/myrepo",
            "description": "desc",
            "web_url": "https://gitea.example.com/myorg/myrepo",
            "inferred_base": "https://gitea.example.com",
            "default_branch": "main",
            "langs_raw": {},
        }

        resp = self.client.post(
            self._url(),
            data={"repo_full_name": "myorg/myrepo", "organization_id": org.id},
            format="json",
        )
        self.assertEqual(resp.status_code, 409)


class GiteaProjectsListViewTests(TestCase):
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
            integration_type=OrgIntegrationType.GITEA,
            name="Gitea",
            config={"base_url": "https://gitea.example.com"},
            secret=TEST_GITEA_TOKEN,
            is_active=True,
        )

    def _url(self):
        return reverse("aist:gitea_projects_list")

    @patch("aist.views.integrations.fetch_gitea_projects.delay")
    def test_list_returns_projects(self, mock_delay):
        mock_delay.return_value.get.return_value = {
            "ok": True,
            "projects": [{"name": "myrepo", "full_name": "myorg/myrepo"}],
        }
        resp = self.client.post(self._url(), data={"organization_id": self.org.id})
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json()["ok"])
        self.assertEqual(resp.json()["projects"][0]["full_name"], "myorg/myrepo")

    def test_list_requires_organization(self):
        resp = self.client.post(self._url(), data={})
        self.assertEqual(resp.status_code, 400)

    def test_list_requires_active_integration(self):
        other_org = Organization.objects.create(name="No Integration Org")
        resp = self.client.post(self._url(), data={"organization_id": other_org.id})
        self.assertEqual(resp.status_code, 404)

    @patch("aist.views.integrations.fetch_gitea_projects.delay")
    def test_list_task_error_is_logged_not_swallowed(self, mock_delay):
        """
        The real exception (auth failure, DNS error, VPN sidecar failure, ...) must
        reach the server logs — the client only ever sees a generic message, but
        silently dropping the cause makes production failures undiagnosable.
        """
        mock_delay.return_value.get.side_effect = RuntimeError("connection refused to gitea.example.com")

        with self.assertLogs("aist.views.integrations", level="ERROR") as logs:
            resp = self.client.post(self._url(), data={"organization_id": self.org.id})

        self.assertEqual(resp.status_code, 502)
        self.assertIn("Gitea project listing failed", "\n".join(logs.output))


class GiteaIntegrationValidateTests(TestCase):

    """Unit-level coverage of the GITEA branch in ``_validate_integration``."""

    def setUp(self):
        self.org = Organization.objects.create(name="Validate Org")
        self.integration = OrgIntegration.objects.create(
            organization=self.org,
            integration_type=OrgIntegrationType.GITEA,
            name="Gitea",
            config={"base_url": "https://gitea.example.com"},
            secret=TEST_GITEA_TOKEN,
            is_active=True,
        )

    @patch("requests.Session.get")
    def test_validate_success_calls_repos_search_endpoint(self, mock_get):
        """
        Uses /api/v1/repos/search (needs only "read:repository"), not /api/v1/user
        (needs "read:user") — a token scoped only for repo access, which is all this
        integration actually uses, would otherwise fail validation with a 403.
        """
        from aist.api.org_integrations import _validate_integration  # noqa: PLC0415

        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_get.return_value = mock_resp

        valid, detail = _validate_integration(self.integration)

        self.assertTrue(valid)
        self.assertEqual(detail, "")
        called_url = mock_get.call_args[0][0]
        self.assertEqual(called_url, "https://gitea.example.com/api/v1/repos/search")
        called_headers = mock_get.call_args.kwargs["headers"]
        self.assertEqual(called_headers, {"Authorization": f"token {TEST_GITEA_TOKEN}"})

    @patch("requests.Session.get")
    def test_validate_auth_failure_returns_invalid(self, mock_get):
        import requests

        from aist.api.org_integrations import _validate_integration  # noqa: PLC0415

        mock_resp = MagicMock()
        mock_resp.raise_for_status.side_effect = requests.HTTPError("401 Unauthorized")
        mock_get.return_value = mock_resp

        valid, detail = _validate_integration(self.integration)

        self.assertFalse(valid)
        self.assertIn("HTTPError", detail)

    def test_validate_missing_base_url_returns_invalid_without_network_call(self):
        from aist.api.org_integrations import _validate_integration  # noqa: PLC0415

        self.integration.config = {}
        self.integration.save(update_fields=["config"])

        valid, detail = _validate_integration(self.integration)

        self.assertFalse(valid)
        self.assertIn("base_url", detail)


class FetchGiteaProjectsTaskTests(TestCase):

    """
    Direct unit coverage of the ``fetch_gitea_projects`` Celery task's HTTP calls —
    guards against regressing to an endpoint that needs a scope ("read:user") a
    repo-scoped token won't have, and against assuming the wrong response shape
    (/api/v1/repos/search wraps results in {"ok": ..., "data": [...]}, unlike
    /api/v1/user/repos which returns a bare array).
    """

    def setUp(self):
        self.org = Organization.objects.create(name="Fetch Projects Org")
        self.integration = OrgIntegration.objects.create(
            organization=self.org,
            integration_type=OrgIntegrationType.GITEA,
            name="Gitea",
            config={"base_url": "https://gitea.example.com"},
            secret=TEST_GITEA_TOKEN,
            is_active=True,
        )

    @patch("requests.Session.get")
    def test_calls_repos_search_and_parses_data_wrapper(self, mock_get):
        from aist.tasks.integrations import fetch_gitea_projects  # noqa: PLC0415

        search_resp = MagicMock()
        search_resp.raise_for_status = MagicMock()
        search_resp.json.return_value = {
            "ok": True,
            "data": [{
                "id": 5,
                "name": "myrepo",
                "full_name": "myorg/myrepo",
                "description": "desc",
                "html_url": "https://gitea.example.com/myorg/myrepo",
                "default_branch": "main",
                "private": False,
            }],
        }
        langs_resp = MagicMock()
        langs_resp.raise_for_status = MagicMock()
        langs_resp.json.return_value = {"Python": 100}

        # First call: repo search page (5 results < limit=50, no second page needed).
        # Second call: per-repo languages lookup.
        mock_get.side_effect = [search_resp, langs_resp]

        result = fetch_gitea_projects.run(self.integration.pk)

        self.assertTrue(result["ok"])
        self.assertEqual(len(result["projects"]), 1)
        self.assertEqual(result["projects"][0]["full_name"], "myorg/myrepo")
        self.assertEqual(result["projects"][0]["language"], "Python")

        first_call_url = mock_get.call_args_list[0][0][0]
        self.assertEqual(first_call_url, "https://gitea.example.com/api/v1/repos/search")
        self.assertNotIn("/api/v1/user/repos", first_call_url)
