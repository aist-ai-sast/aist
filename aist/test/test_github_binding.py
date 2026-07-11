from __future__ import annotations

from unittest.mock import Mock, patch

from django.test import TestCase
from django_github_app.models import Installation

from aist.models import RepositoryInfo, ScmGithubBinding, ScmType


class GithubBindingTests(TestCase):
    def setUp(self):
        self.repo = RepositoryInfo.objects.create(
            type=ScmType.GITHUB,
            repo_owner="owner",
            repo_name="repo",
            base_url="https://github.com",
        )
        self.binding = ScmGithubBinding.objects.create(scm=self.repo, installation_id=12345)

    @patch("aist.models.async_to_sync")
    def test_get_project_info_returns_data(self, mock_async_to_sync):
        Installation.objects.create(installation_id=12345, data={"app_slug": "aist-app"})
        runner = Mock(return_value={"default_branch": "main"})
        mock_async_to_sync.return_value = runner

        info = self.binding.get_project_info(self.repo)

        self.assertEqual(info, {"default_branch": "main"})

    def test_get_project_info_returns_none_without_installation_id(self):
        self.binding.installation_id = None
        self.binding.save(update_fields=["installation_id"])

        info = self.binding.get_project_info(self.repo)

        self.assertIsNone(info)

    @patch("aist.models.async_to_sync")
    def test_build_clone_url_uses_installation_token(self, mock_async_to_sync):
        Installation.objects.create(installation_id=12345, data={"app_slug": "aist-app"})
        runner = Mock(return_value="token-123")
        mock_async_to_sync.return_value = runner

        clone_url = self.binding.build_clone_url(self.repo)

        self.assertIn("x-access-token:token-123@", clone_url)
        self.assertIn("/owner/repo.git", clone_url)

    @patch("aist.models.async_to_sync")
    def test_build_clone_url_embeds_token_on_plain_http_host(self, mock_async_to_sync):
        # GitHub Enterprise Server is sometimes reachable only over http:// on
        # an internal network — the token must still be embedded, not silently
        # dropped (regression: a hardcoded "https://" replace was a no-op here).
        Installation.objects.create(installation_id=12345, data={"app_slug": "aist-app"})
        mock_async_to_sync.return_value = Mock(return_value="token-123")
        self.repo.base_url = "http://ghe.internal"
        self.repo.save(update_fields=["base_url"])

        clone_url = self.binding.build_clone_url(self.repo)

        self.assertEqual(clone_url, "http://x-access-token:token-123@ghe.internal/owner/repo.git")

    @patch("aist.models.async_to_sync")
    def test_get_auth_headers_uses_installation_token(self, mock_async_to_sync):
        Installation.objects.create(installation_id=12345, data={"app_slug": "aist-app"})
        runner = Mock(return_value="token-abc")
        mock_async_to_sync.return_value = runner

        headers = self.binding.get_auth_headers()

        self.assertEqual(headers, {"Authorization": "token token-abc"})

    @patch("aist.models.async_to_sync")
    def test_get_project_info_returns_none_when_fetch_fails(self, mock_async_to_sync):
        Installation.objects.create(installation_id=12345, data={"app_slug": "aist-app"})
        mock_async_to_sync.return_value = Mock(side_effect=RuntimeError("boom"))

        info = self.binding.get_project_info(self.repo)

        self.assertIsNone(info)
