"""
Source-file view of a finding: the file blob endpoint reads one file of a project version.

Scenarios: a reviewer opens a finding's code snippet from an uploaded archive or from the project's
repository; a crafted path (``..``, its percent-encoded form, an absolute path or a symlink) or a crafted
version ref never leaves the version's source, and never reaches another repository the integration
credentials can read.
"""
from __future__ import annotations

from unittest.mock import patch

import requests
from django.core.files.uploadedfile import SimpleUploadedFile

from aist.models import (
    AISTProjectVersion,
    RepositoryInfo,
    ScmGithubBinding,
    ScmType,
    VersionType,
)
from aist.test.source_version_fixtures import COMMIT, OUTSIDE_CONTENT, FakeScmHost, SourceVersionMixin
from aist.test.test_api import AISTApiBase

INSTALLATION_TOKEN = "ghs_test_installation_token"  # noqa: S105 -- test fixture
INSTALLATION_AUTH = {"Authorization": f"token {INSTALLATION_TOKEN}"}


class ArchiveFileBlobTests(SourceVersionMixin, AISTApiBase):
    def setUp(self):
        super().setUp()
        self.pv = self.archive_version({"app/views.py": "def index():\n    return render()\n", "README.md": "# widgets\n"})
        self.root = self.pv.ensure_extracted()
        # A sibling of the version's extraction directory, i.e. outside its source.
        self.outside = self.root.parent / "secret.py"
        self.outside.write_text(OUTSIDE_CONTENT, encoding="utf-8")

    def test_file_inside_the_archive_is_served(self):
        resp = self.client.get(self.blob_url(self.pv, "app/views.py"))

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(b"".join(resp.streaming_content), b"def index():\n    return render()\n")
        self.assertEqual(resp["Content-Type"], "text/x-python")
        self.assertEqual(resp["Content-Disposition"], 'inline; filename="views.py"')

    def test_path_leaving_the_archive_is_404(self):
        for raw_subpath in ("../secret.py", "%2e%2e/secret.py", "app/%2E%2E/../secret.py", "..%5Csecret.py"):
            with self.subTest(raw_subpath=raw_subpath):
                resp = self.client.get(self.blob_url(self.pv, raw_subpath))

                self.assertEqual(resp.status_code, 404)
                self.assertNotIn(OUTSIDE_CONTENT.encode(), resp.content)

    def test_absolute_path_is_404(self):
        resp = self.client.get(self.blob_url(self.pv, str(self.outside)))

        self.assertEqual(resp.status_code, 404)
        self.assertNotIn(OUTSIDE_CONTENT.encode(), resp.content)

    def test_symlink_pointing_outside_the_archive_is_404(self):
        (self.root / "app" / "linked.py").symlink_to(self.outside)

        resp = self.client.get(self.blob_url(self.pv, "app/linked.py"))

        self.assertEqual(resp.status_code, 404)
        self.assertNotIn(OUTSIDE_CONTENT.encode(), resp.content)

    def test_archive_that_cannot_be_extracted_serves_no_file(self):
        pv = AISTProjectVersion.objects.create(
            project=self.project,
            version_type=VersionType.FILE_HASH,
            source_archive=SimpleUploadedFile("src.zip", b"plain text, neither zip nor tar"),
        )

        resp = self.client.get(self.blob_url(pv, "app/views.py"))

        self.assertEqual(resp.status_code, 404)


class RepositoryFileBlobTests(SourceVersionMixin, AISTApiBase):
    def _github_version_with_installation(self, **repository_fields) -> AISTProjectVersion:
        repository = RepositoryInfo.objects.create(
            type=ScmType.GITHUB, repo_owner="acme", repo_name="widgets", **repository_fields,
        )
        ScmGithubBinding.objects.create(scm=repository, installation_id=4242)
        return self.repository_version(repository)

    @patch.object(ScmGithubBinding, "get_auth_headers", return_value=INSTALLATION_AUTH)
    def test_path_leaving_the_repository_is_404_and_never_requested(self, mock_auth):
        pv = self._github_version_with_installation()

        with FakeScmHost() as host:
            for raw_subpath in ("../../acme/payroll/main/.env", "%2e%2e/%2e%2e/acme/payroll/main/.env", "/etc/hostname"):
                with self.subTest(raw_subpath=raw_subpath):
                    resp = self.client.get(self.blob_url(pv, raw_subpath))

                    self.assertEqual(resp.status_code, 404)
        self.assertEqual(host.requests, [])

    @patch.object(ScmGithubBinding, "get_auth_headers", return_value=INSTALLATION_AUTH)
    def test_stored_ref_leaving_the_repository_is_404_and_never_requested(self, mock_auth):
        for repository_fields in ({}, {"base_url": "https://github.example.com"}):
            with self.subTest(repository=repository_fields):
                pv = self._github_version_with_installation(**repository_fields)
                for version_type, ref in (
                    (VersionType.GIT_HASH, "../../acme/payroll/main"),
                    (VersionType.GIT_HASH, "../../../api/v3/user"),
                    (VersionType.GIT_BRANCH, "main/../../payroll"),
                ):
                    # Stored before refs were validated at creation.
                    AISTProjectVersion.objects.filter(pk=pv.pk).update(version_type=version_type, version=ref)
                    pv.refresh_from_db()
                    with FakeScmHost(body=OUTSIDE_CONTENT.encode()) as host:
                        resp = self.client.get(self.blob_url(pv, "README.md"))

                    self.assertEqual(resp.status_code, 404)
                    self.assertEqual(host.requests, [])
                pv.delete()

    @patch.object(ScmGithubBinding, "get_auth_headers", return_value=INSTALLATION_AUTH)
    def test_installation_fetch_sends_the_token_and_encodes_the_path(self, mock_auth):
        pv = self._github_version_with_installation()
        cases = {
            "docs/a%3Fb%23c.md": f"https://raw.githubusercontent.com/acme/widgets/{COMMIT}/docs/a%3Fb%23c.md",
            "%252e%252e/x.md": f"https://raw.githubusercontent.com/acme/widgets/{COMMIT}/%252e%252e/x.md",
        }
        for raw_subpath, expected_url in cases.items():
            with self.subTest(raw_subpath=raw_subpath), FakeScmHost(body=b"# notes\n") as host:
                resp = self.client.get(self.blob_url(pv, raw_subpath))

                self.assertEqual(resp.status_code, 200)
                self.assertEqual(b"".join(resp.streaming_content), b"# notes\n")
                self.assertEqual(host.last.url, expected_url)
                self.assertEqual(host.last.headers["Authorization"], f"token {INSTALLATION_TOKEN}")

    def test_branch_version_reads_its_last_resolved_commit_else_the_branch(self):
        repository = RepositoryInfo.objects.create(type=ScmType.GITHUB, repo_owner="acme", repo_name="widgets")
        resolved = "b" * 40
        cases = (
            ("main", resolved, f"https://raw.githubusercontent.com/acme/widgets/{resolved}/src/app.py"),
            ("develop", "", "https://raw.githubusercontent.com/acme/widgets/develop/src/app.py"),
        )
        for branch, last_resolved_commit, expected_url in cases:
            with self.subTest(branch=branch):
                pv = self.repository_version(
                    repository, VersionType.GIT_BRANCH, branch, last_resolved_commit=last_resolved_commit,
                )
                with FakeScmHost(body=b"print('ok')\n") as host:
                    resp = self.client.get(self.blob_url(pv, "src/app.py"))

                self.assertEqual(resp.status_code, 200)
                self.assertEqual(host.last.url, expected_url)

    def test_public_repository_without_binding_is_read_anonymously_from_its_host(self):
        cases = (
            (
                {"type": ScmType.GITHUB},
                f"https://raw.githubusercontent.com/acme/widgets/{COMMIT}/src/app.py",
            ),
            (
                {"type": ScmType.GITHUB, "base_url": "https://github.example.com/"},
                f"https://github.example.com/acme/widgets/raw/{COMMIT}/src/app.py",
            ),
            (
                {"type": ScmType.GITLAB, "base_url": "https://gitlab.example.com"},
                f"https://gitlab.example.com/api/v4/projects/acme%2Fwidgets/repository/files/src%2Fapp.py/raw?ref={COMMIT}",
            ),
            (
                {"type": ScmType.GITEA, "base_url": "https://gitea.example.com"},
                f"https://gitea.example.com/api/v1/repos/acme/widgets/raw/src%2Fapp.py?ref={COMMIT}",
            ),
        )
        for repository_fields, expected_url in cases:
            with self.subTest(repository=repository_fields):
                repository = RepositoryInfo.objects.create(repo_owner="acme", repo_name="widgets", **repository_fields)
                pv = self.repository_version(repository)

                with FakeScmHost(body=b"print('ok')\n") as host:
                    resp = self.client.get(self.blob_url(pv, "src/app.py"))

                self.assertEqual(resp.status_code, 200)
                self.assertEqual(b"".join(resp.streaming_content), b"print('ok')\n")
                self.assertEqual([request.url for request in host.requests], [expected_url])
                self.assertFalse({"Authorization", "PRIVATE-TOKEN"} & set(host.last.headers))
                self.assertEqual(host.last.proxies, {})

    def test_gerrit_repository_without_binding_has_no_public_fallback(self):
        repository = RepositoryInfo.objects.create(
            type=ScmType.GERRIT, repo_owner="platform", repo_name="server", base_url="https://gerrit.example.com",
        )
        pv = self.repository_version(repository)

        with FakeScmHost() as host:
            resp = self.client.get(self.blob_url(pv, "src/main.c"))

        self.assertEqual(resp.status_code, 404)
        self.assertEqual(host.requests, [])

    def test_missing_file_on_the_host_is_404(self):
        for repository_fields in ({"type": ScmType.GITHUB}, {"type": ScmType.GITLAB, "base_url": "https://gitlab.example.com"}):
            with self.subTest(repository=repository_fields):
                repository = RepositoryInfo.objects.create(repo_owner="acme", repo_name="widgets", **repository_fields)
                pv = self.repository_version(repository)

                with FakeScmHost(body=b'{"message":"404 File Not Found"}', status_code=404):
                    resp = self.client.get(self.blob_url(pv, "src/gone.py"))

                self.assertEqual(resp.status_code, 404)

    def test_vpn_gated_fetch_goes_through_the_egress_proxy(self):
        proxy = "http://egress-proxy:1080"
        for repository_fields in (
            {"type": ScmType.GITLAB, "base_url": "https://gitlab.internal.example"},
            {"type": ScmType.GITEA, "base_url": "https://gitea.internal.example"},
        ):
            with self.subTest(repository=repository_fields):
                repository = RepositoryInfo.objects.create(repo_owner="acme", repo_name="widgets", **repository_fields)
                pv = self.repository_version(repository)

                with (
                    patch("aist.api.files.egress.proxy_url_for_project_version", return_value=proxy),
                    FakeScmHost(body=b"x = 1\n") as host,
                ):
                    resp = self.client.get(self.blob_url(pv, "src/app.py"))

                self.assertEqual(resp.status_code, 200)
                self.assertEqual(host.last.proxies["https"], proxy)

    def test_unreachable_public_host_propagates(self):
        repository = RepositoryInfo.objects.create(type=ScmType.GITHUB, repo_owner="acme", repo_name="widgets")
        pv = self.repository_version(repository)

        with FakeScmHost(error=requests.ConnectionError("unreachable")), self.assertRaises(requests.ConnectionError):
            pv.open_source_file("src/app.py")


class ProjectVersionWithoutRepositoryTests(SourceVersionMixin, AISTApiBase):
    def test_git_version_without_repository_is_404(self):
        self.assertIsNone(self.project.repository)
        pv = AISTProjectVersion.objects.create(project=self.project, version_type=VersionType.GIT_HASH, version=COMMIT)

        with FakeScmHost() as host:
            resp = self.client.get(self.blob_url(pv, "src/app.py"))

        self.assertEqual(resp.status_code, 404)
        self.assertEqual(host.requests, [])
