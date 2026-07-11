from __future__ import annotations

import base64
from unittest.mock import Mock, patch

from django.test import TestCase
from dojo.models import Product_Type

from aist.models import Organization, OrgIntegration, RepositoryInfo, ScmGerritBinding, ScmType


class GerritBindingTests(TestCase):
    def setUp(self):
        # Gerrit project "platform/build/soong" split by last "/" on import.
        self.repo = RepositoryInfo.objects.create(
            type=ScmType.GERRIT,
            repo_owner="platform/build",
            repo_name="soong",
            base_url="https://gerrit.example.com",
        )
        org = Organization.objects.create(
            name="Gerrit Test Org",
            product_type=Product_Type.objects.create(name="Gerrit PT"),
        )
        self.integration = OrgIntegration.objects.create(
            organization=org,
            integration_type="GERRIT",
            name="Test Gerrit",
            config={"username": "svc-user"},
            secret="httppass",  # noqa: S106 -- test fixture
        )
        self.binding = ScmGerritBinding.objects.create(scm=self.repo, org_integration=self.integration)

    def test_repo_full_reconstructs_full_gerrit_path(self):
        self.assertEqual(self.repo.repo_full, "platform/build/soong")

    def test_build_clone_url_embeds_credentials_and_a_prefix(self):
        url = self.binding.build_clone_url(self.repo)
        self.assertEqual(
            url,
            "https://svc-user:httppass@gerrit.example.com/a/platform/build/soong",
        )
        # clone_url on the repo delegates to the binding.
        self.assertEqual(self.repo.clone_url, url)

    def test_single_segment_project_has_no_leading_slash(self):
        # Gerrit projects like "All-Projects" have no "/" → owner empty.
        repo = RepositoryInfo.objects.create(
            type=ScmType.GERRIT,
            repo_owner="",
            repo_name="All-Projects",
            base_url="https://gerrit.example.com",
        )
        binding = ScmGerritBinding.objects.create(scm=repo, org_integration=self.integration)
        self.assertEqual(
            binding.build_clone_url(repo),
            "https://svc-user:httppass@gerrit.example.com/a/All-Projects",
        )
        self.assertIn("/a/projects/All-Projects/", binding.build_raw_url(repo, "main", "f.c"))

    def test_build_clone_url_embeds_credentials_on_plain_http_host(self):
        # Self-hosted Gerrit is frequently reachable only over http:// on an
        # internal network — credentials must still be embedded, not silently
        # dropped (regression: a hardcoded "https://" replace was a no-op here).
        self.repo.base_url = "http://gerrit.internal:8080"
        self.repo.save(update_fields=["base_url"])
        url = self.binding.build_clone_url(self.repo)
        self.assertEqual(
            url,
            "http://svc-user:httppass@gerrit.internal:8080/a/platform/build/soong",
        )

    def test_build_clone_url_none_without_credentials(self):
        self.binding.org_integration.secret = ""
        self.assertIsNone(self.binding.build_clone_url(self.repo))
        self.binding.org_integration.secret = "httppass"  # noqa: S105 -- test fixture
        self.binding.org_integration.config = {}
        self.assertIsNone(self.binding.build_clone_url(self.repo))

    def test_build_raw_url_targets_gerrit_content_endpoint(self):
        url = self.binding.build_raw_url(self.repo, "main", "src/a.c")
        self.assertEqual(
            url,
            "https://gerrit.example.com/a/projects/platform%2Fbuild%2Fsoong"
            "/branches/main/files/src%2Fa.c/content",
        )

    def test_get_auth_headers_is_http_basic(self):
        headers = self.binding.get_auth_headers()
        expected = base64.b64encode(b"svc-user:httppass").decode()
        self.assertEqual(headers, {"Authorization": f"Basic {expected}"})

    def test_get_auth_headers_empty_without_credentials(self):
        self.binding.org_integration.config = {}
        self.assertEqual(self.binding.get_auth_headers(), {})

    @patch("aist.models.base64.b64decode")
    def test_fetch_raw_bytes_decodes_base64(self, mock_b64decode):
        mock_b64decode.return_value = b"int main() {}"
        with patch("requests.get") as mock_get:
            mock_get.return_value = Mock(status_code=200, text="aW50IG1haW4oKSB7fQ==")
            mock_get.return_value.raise_for_status = Mock()
            data = self.binding.fetch_raw_bytes(self.repo, "main", "src/a.c")
        self.assertEqual(data, b"int main() {}")

    def test_fetch_raw_bytes_returns_none_on_404(self):
        with patch("requests.get") as mock_get:
            mock_get.return_value = Mock(status_code=404)
            self.assertIsNone(self.binding.fetch_raw_bytes(self.repo, "main", "missing.c"))

    @patch("pygerrit2.GerritRestAPI")
    def test_get_project_info_returns_default_branch(self, mock_rest):
        mock_rest.return_value.get.return_value = "refs/heads/main"
        info = self.binding.get_project_info(self.repo)
        self.assertEqual(info, {"default_branch": "main"})

    @patch("pygerrit2.GerritRestAPI")
    def test_get_project_info_handles_error(self, mock_rest):
        mock_rest.return_value.get.side_effect = RuntimeError("boom")
        self.assertIsNone(self.binding.get_project_info(self.repo))
