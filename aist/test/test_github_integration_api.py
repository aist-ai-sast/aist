from __future__ import annotations

from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlparse

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from dojo.models import Product, Product_Type, SLA_Configuration
from rest_framework.test import APIClient

from aist.api.github_integration import _fetch_repository_details
from aist.models import (
    AISTProject,
    AISTProjectVersion,
    Organization,
    RepositoryInfo,
    ScmGithubBinding,
    ScmType,
    VersionType,
)


class _AnalyzerConfigStub:
    @staticmethod
    def convert_languages(_languages):
        return ["python"]


class GithubIntegrationAPITests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = get_user_model().objects.create_superuser(
            username="gh-admin",
            email="gh-admin@example.com",
            password="pass",  # noqa: S106
        )
        self.client.force_authenticate(user=self.user)
        self.sla, _ = SLA_Configuration.objects.get_or_create(id=1, defaults={"name": "SLA default"})

    def test_github_import_options_returns_organizations(self):
        Organization.objects.create(name="Org One")
        Organization.objects.create(name="Org Two")

        resp = self.client.get(reverse("aist_api:github_import_options"))

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(resp.data["organizations"]), 2)

    @patch("aist.api.github_integration.settings.GITHUB_APP", {"NAME": "aist-app"})
    def test_connect_start_returns_github_redirect_url(self):
        org = Organization.objects.create(name="Org Connect")

        resp = self.client.post(
            reverse("aist_api:github_import_connect_start"),
            data={"organization_id": org.id},
            format="json",
        )

        self.assertEqual(resp.status_code, 200)
        redirect_url = resp.data["redirect_url"]
        self.assertIn("https://github.com/apps/aist-app/installations/new", redirect_url)
        parsed = urlparse(redirect_url)
        self.assertIn("state", parse_qs(parsed.query))

    @patch("aist.api.github_integration.settings.GITHUB_APP", {"NAME": "aist-app"})
    def test_connect_callback_redirects_with_installation(self):
        org = Organization.objects.create(name="Org Callback")
        start_resp = self.client.post(
            reverse("aist_api:github_import_connect_start"),
            data={"organization_id": org.id},
            format="json",
        )
        state = parse_qs(urlparse(start_resp.data["redirect_url"]).query)["state"][0]

        callback_resp = self.client.get(
            reverse("aist_api:github_connect_callback"),
            data={"state": state, "installation_id": 1001},
        )

        self.assertEqual(callback_resp.status_code, 302)
        self.assertIn("github_installation_id=1001", callback_resp.url)

    @patch("aist.api.github_integration.settings.GITHUB_APP", {"NAME": "aist-app"})
    def test_connect_callback_rejects_reused_state(self):
        org = Organization.objects.create(name="Org Callback Replay")
        start_resp = self.client.post(
            reverse("aist_api:github_import_connect_start"),
            data={"organization_id": org.id},
            format="json",
        )
        state = parse_qs(urlparse(start_resp.data["redirect_url"]).query)["state"][0]

        first_resp = self.client.get(
            reverse("aist_api:github_connect_callback"),
            data={"state": state, "installation_id": 1001},
        )
        self.assertEqual(first_resp.status_code, 302)

        second_resp = self.client.get(
            reverse("aist_api:github_connect_callback"),
            data={"state": state, "installation_id": 1001},
        )
        self.assertEqual(second_resp.status_code, 400)
        self.assertIn("state", second_resp.data)

    @patch("aist.api.github_integration._list_installation_repositories")
    def test_import_repositories_returns_repositories(self, mock_list_repos):
        org = Organization.objects.create(name="Org Repos")
        org.ensure_product_type()
        mock_list_repos.return_value = [
            {"id": 1, "full_name": "owner/repo", "private": True, "default_branch": "main"},
        ]

        resp = self.client.get(
            reverse("aist_api:github_import_repositories"),
            data={"organization_id": org.id, "installation_id": 77},
        )

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data["installation_id"], 77)
        self.assertEqual(len(resp.data["repositories"]), 1)

    @patch("aist.api.github_integration._fetch_repository_details")
    @patch("aist.api.github_integration._list_installation_repositories")
    @patch("aist.api.github_integration._load_analyzers_config")
    def test_import_execute_creates_project_and_binding(self, mock_cfg, mock_list_repos, mock_fetch_details):
        org = Organization.objects.create(name="Org Import")
        mock_cfg.return_value = _AnalyzerConfigStub()
        mock_list_repos.return_value = [
            {"id": 1, "full_name": "owner/repo", "private": True, "default_branch": "main"},
        ]
        mock_fetch_details.return_value = (
            {
                "html_url": "https://github.com/owner/repo",
                "description": "repo desc",
            },
            {"Python": 100},
        )

        resp = self.client.post(
            reverse("aist_api:github_import_execute"),
            data={"organization_id": org.id, "installation_id": 77, "repositories": ["owner/repo"]},
            format="json",
        )

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(resp.data["imported"]), 1)

        repo = RepositoryInfo.objects.get(type=ScmType.GITHUB, repo_owner="owner", repo_name="repo")
        binding = ScmGithubBinding.objects.get(scm=repo)
        self.assertEqual(binding.installation_id, 77)

        project = AISTProject.objects.get(repository=repo)
        self.assertEqual(project.organization_id, org.id)

    @patch("aist.api.github_integration._fetch_repository_details")
    @patch("aist.api.github_integration._list_installation_repositories")
    @patch("aist.api.github_integration._load_analyzers_config")
    def test_import_seeds_real_default_branch_not_hardcoded_master(self, mock_cfg, mock_list_repos, mock_fetch_details):
        """Regression: initial version must use the real default branch, not a hardcoded "master"."""
        org = Organization.objects.create(name="Org DefaultBranch")
        mock_cfg.return_value = _AnalyzerConfigStub()
        mock_list_repos.return_value = [
            {"id": 1, "full_name": "owner/repo-db", "private": True, "default_branch": "main"},
        ]
        mock_fetch_details.return_value = (
            {
                "html_url": "https://github.com/owner/repo-db",
                "description": "repo desc",
                "default_branch": "main",
            },
            {"Python": 100},
        )

        resp = self.client.post(
            reverse("aist_api:github_import_execute"),
            data={"organization_id": org.id, "installation_id": 77, "repositories": ["owner/repo-db"]},
            format="json",
        )

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(resp.data["imported"]), 1)

        repo = RepositoryInfo.objects.get(type=ScmType.GITHUB, repo_owner="owner", repo_name="repo-db")
        project = AISTProject.objects.get(repository=repo)
        versions = list(AISTProjectVersion.objects.filter(project=project))
        self.assertEqual(len(versions), 1)
        self.assertEqual(versions[0].version, "main")
        self.assertEqual(versions[0].version_type, VersionType.GIT_BRANCH)

    @patch("aist.api.github_integration._import_github_repository")
    @patch("aist.api.github_integration._list_installation_repositories")
    def test_import_execute_returns_failed_item_details(self, mock_list_repos, mock_import_repo):
        org = Organization.objects.create(name="Org Import Failed")
        mock_list_repos.return_value = [
            {"id": 1, "full_name": "owner/repo-fail", "private": True, "default_branch": "main"},
        ]
        mock_import_repo.side_effect = RuntimeError("upstream api timeout")

        resp = self.client.post(
            reverse("aist_api:github_import_execute"),
            data={"organization_id": org.id, "installation_id": 77, "repositories": ["owner/repo-fail"]},
            format="json",
        )

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data["imported"], [])
        self.assertEqual(len(resp.data["failed"]), 1)
        self.assertEqual(resp.data["failed"][0]["repo"], "owner/repo-fail")
        self.assertEqual(resp.data["failed"][0]["reason"], "import_failed")
        self.assertEqual(resp.data["failed"][0]["detail"], "upstream api timeout")

    @patch("aist.api.github_integration._fetch_repository_details")
    @patch("aist.api.github_integration._list_installation_repositories")
    @patch("aist.api.github_integration._load_analyzers_config")
    def test_project_link_repository_updates_existing_project(self, mock_cfg, mock_list_repos, mock_fetch_details):
        product_type = Product_Type.objects.create(name="PT")
        product = Product.objects.create(
            name="My Product",
            description="desc",
            prod_type=product_type,
            sla_configuration_id=self.sla.id,
        )
        project = AISTProject.objects.create(
            product=product,
            supported_languages=[],
            compilable=False,
            profile={},
        )

        mock_cfg.return_value = _AnalyzerConfigStub()
        mock_list_repos.return_value = [
            {"id": 1, "full_name": "owner/repo-link", "private": True, "default_branch": "main"},
        ]
        mock_fetch_details.return_value = (
            {
                "html_url": "https://github.com/owner/repo-link",
                "description": "new desc",
            },
            {"Python": 100},
        )

        resp = self.client.post(
            reverse("aist_api:project_github_link_repository", kwargs={"project_id": project.id}),
            data={"installation_id": 88, "repository_full_name": "owner/repo-link"},
            format="json",
        )

        self.assertEqual(resp.status_code, 200)
        project.refresh_from_db()
        self.assertIsNotNone(project.repository)
        self.assertEqual(project.repository.repo_full, "owner/repo-link")
        self.assertEqual(project.supported_languages, ["python"])

    @patch("aist.api.github_integration.async_to_sync")
    @patch("aist.api.github_integration._afetch_repository_details")
    @patch("aist.api.github_integration._ensure_installation_exists")
    def test_fetch_repository_details_uses_async_github_client(
        self,
        mock_ensure,
        mock_afetch,
        mock_async_to_sync,
    ):
        installation = Mock()
        mock_ensure.return_value = installation
        runner = Mock(return_value=({"html_url": "https://github.com/owner/repo"}, {"Python": 100}))
        mock_async_to_sync.return_value = runner

        details, languages = _fetch_repository_details(77, "owner/repo")

        self.assertEqual(details["html_url"], "https://github.com/owner/repo")
        self.assertEqual(languages, {"Python": 100})
        mock_async_to_sync.assert_called_once_with(mock_afetch)
        runner.assert_called_once_with(installation, "owner/repo")
