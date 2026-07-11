from __future__ import annotations

from unittest.mock import Mock, patch

from django.test import TestCase
from dojo.models import Product_Type

from aist.models import Organization, OrgIntegration, RepositoryInfo, ScmGiteaBinding, ScmType


class GiteaBindingTests(TestCase):
    def setUp(self):
        self.repo = RepositoryInfo.objects.create(
            type=ScmType.GITEA,
            repo_owner="myorg",
            repo_name="myrepo",
            base_url="https://gitea.example.com",
        )
        org = Organization.objects.create(
            name="Gitea Test Org",
            product_type=Product_Type.objects.create(name="Gitea PT"),
        )
        self.integration = OrgIntegration.objects.create(
            organization=org,
            integration_type="GITEA",
            name="Test Gitea",
            config={"base_url": "https://gitea.example.com"},
            secret="pat-token-123",  # noqa: S106 -- test fixture
        )
        self.binding = ScmGiteaBinding.objects.create(scm=self.repo, org_integration=self.integration)

    def test_repo_full_is_owner_slash_repo(self):
        self.assertEqual(self.repo.repo_full, "myorg/myrepo")

    def test_build_clone_url_embeds_token_as_username(self):
        url = self.binding.build_clone_url(self.repo)
        self.assertEqual(url, "https://pat-token-123@gitea.example.com/myorg/myrepo.git")
        # clone_url on the repo delegates to the binding.
        self.assertEqual(self.repo.clone_url, url)

    def test_build_clone_url_none_without_token(self):
        self.binding.org_integration.secret = ""
        self.assertIsNone(self.binding.build_clone_url(self.repo))

    def test_build_clone_url_embeds_token_on_plain_http_host(self):
        # Self-hosted Gitea is frequently reachable only over http:// on an
        # internal network — the token must still be embedded, not silently
        # dropped (regression: a hardcoded "https://" replace was a no-op here).
        self.repo.base_url = "http://10.2.40.158:3000"
        self.repo.save(update_fields=["base_url"])
        url = self.binding.build_clone_url(self.repo)
        self.assertEqual(url, "http://pat-token-123@10.2.40.158:3000/myorg/myrepo.git")

    def test_build_blob_url_uses_src_branch(self):
        url = self.binding.build_blob_url(self.repo, "main", "src/a.py")
        self.assertEqual(url, "https://gitea.example.com/myorg/myrepo/src/branch/main/src/a.py")

    def test_build_raw_url_targets_gitea_api_raw_endpoint(self):
        url = self.binding.build_raw_url(self.repo, "main", "src/a.py")
        self.assertEqual(
            url,
            "https://gitea.example.com/api/v1/repos/myorg/myrepo/raw/src%2Fa.py?ref=main",
        )

    def test_get_auth_headers_uses_token_header(self):
        headers = self.binding.get_auth_headers()
        self.assertEqual(headers, {"Authorization": "token pat-token-123"})

    def test_get_auth_headers_empty_without_token(self):
        self.binding.org_integration.secret = ""
        self.assertEqual(self.binding.get_auth_headers(), {})

    @patch("requests.get")
    def test_get_project_info_returns_default_branch(self, mock_get):
        mock_get.return_value = Mock(status_code=200)
        mock_get.return_value.json.return_value = {"default_branch": "main"}
        mock_get.return_value.raise_for_status = Mock()
        info = self.binding.get_project_info(self.repo)
        self.assertEqual(info, {"default_branch": "main"})

    @patch("requests.get")
    def test_get_project_info_handles_error(self, mock_get):
        mock_get.side_effect = RuntimeError("boom")
        self.assertIsNone(self.binding.get_project_info(self.repo))
