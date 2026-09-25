"""
Snippet loading when the SCM refuses or fails the file fetch.

Production scenario: the organization's GitLab PAT expired, GitLab answered every
``/repository/files/.../raw`` request with ``401 {"message":"invalid_token"}`` and the
blob endpoint turned that into a bare 500, so no code snippet loaded anywhere.
"""
from __future__ import annotations

import json
from unittest.mock import patch

import requests
from django.urls import reverse

from aist.api.files import (
    ERR_SCM_AUTH_FAILED,
    ERR_SCM_UNAVAILABLE,
    SCM_AUTH_FAILED_CODE,
    SCM_UNAVAILABLE_CODE,
)
from aist.integrations.scm_errors import ScmFetchError
from aist.models import (
    AISTProjectVersion,
    Organization,
    OrgIntegration,
    OrgIntegrationType,
    RepositoryInfo,
    ScmGerritBinding,
    ScmGitlabBinding,
    ScmType,
    VersionType,
)
from aist.test.test_api import AISTApiBase

EXPIRED_GITLAB_PAT = "xglpat-expiredTOKEN1234567890".removeprefix("x")
GERRIT_HTTP_PASSWORD = "gerrit-http-pass-0123456789"  # noqa: S105 -- test fixture
INTERNAL_GITLAB_HOST = "gitlab.internal.example"
INTERNAL_GERRIT_HOST = "gerrit.internal.example"
COMMIT = "0123456789abcdef0123456789abcdef01234567"


def _scm_response(status_code: int, body: bytes, url: str) -> requests.Response:
    """A real ``requests.Response`` as the SCM would return it (not a Mock)."""
    response = requests.Response()
    response.status_code = status_code
    response._content = body
    response.url = url
    response.headers["Content-Type"] = "application/json"
    return response


class _ScmBlobBase(AISTApiBase):
    def setUp(self):
        super().setUp()
        self.organization = Organization.objects.create(name="SCM Org", product_type=self.prod_type)

    def _git_version(self, repository: RepositoryInfo) -> AISTProjectVersion:
        self.project.repository = repository
        self.project.save(update_fields=["repository"])
        return AISTProjectVersion.objects.create(
            project=self.project,
            version_type=VersionType.GIT_HASH,
            version=COMMIT,
        )

    def _blob_url(self, pv: AISTProjectVersion, subpath: str = "cloud/cloud/settings.py") -> str:
        return reverse("aist_api:project_version_file_blob", kwargs={"project_version_id": pv.id, "subpath": subpath})

    def assertBodyLeaksNothing(self, resp, *secrets: str):
        body = resp.content.decode("utf-8")
        for fragment in (*secrets, COMMIT, "/api/v4/", "?ref=", "/a/projects/", "http://", "https://"):
            self.assertNotIn(fragment, body)


class GitlabExpiredTokenBlobTests(_ScmBlobBase):
    def setUp(self):
        super().setUp()
        integration = OrgIntegration.objects.create(
            organization=self.organization,
            integration_type=OrgIntegrationType.GITLAB,
            name="Corporate GitLab",
            config={"base_url": f"https://{INTERNAL_GITLAB_HOST}"},
            secret=EXPIRED_GITLAB_PAT,
            is_active=True,
        )
        repository = RepositoryInfo.objects.create(
            type=ScmType.GITLAB,
            repo_owner="dev",
            repo_name="cloud_portal",
            base_url=f"https://{INTERNAL_GITLAB_HOST}",
        )
        ScmGitlabBinding.objects.create(scm=repository, org_integration=integration)
        self.pv = self._git_version(repository)
        self.upstream_url = (
            f"https://{INTERNAL_GITLAB_HOST}/api/v4/projects/dev%2Fcloud_portal/repository/files/"
            f"cloud%2Fcloud%2Fsettings.py/raw?ref={COMMIT}"
        )

    def _get_with_upstream(self, status_code: int, body: bytes):
        with patch(
            "aist.api.files.requests.get",
            return_value=_scm_response(status_code, body, self.upstream_url),
        ) as mock_get:
            resp = self.client.get(self._blob_url(self.pv))
        return resp, mock_get

    def test_expired_pat_answers_502_with_an_actionable_message_instead_of_500(self):
        with self.assertLogs("aist.api.files", level="WARNING") as logs:
            resp, mock_get = self._get_with_upstream(401, b'{"message":"invalid_token"}')

        # The PAT was actually sent upstream — the failure is GitLab rejecting it.
        self.assertEqual(mock_get.call_args.kwargs["headers"], {"PRIVATE-TOKEN": EXPIRED_GITLAB_PAT})
        self.assertEqual(resp.status_code, 502)
        payload = json.loads(resp.content)
        self.assertEqual(payload, {"detail": ERR_SCM_AUTH_FAILED, "code": SCM_AUTH_FAILED_CODE})
        self.assertIn("integration settings", payload["detail"])

        log_text = "\n".join(logs.output)
        self.assertIn(f"project_version_id={self.pv.id}", log_text)
        self.assertIn("repo=dev/cloud_portal", log_text)
        self.assertIn("upstream_status=401", log_text)
        self.assertNotIn(EXPIRED_GITLAB_PAT, log_text)
        self.assertNotIn(self.upstream_url, log_text)
        self.assertNotIn(INTERNAL_GITLAB_HOST, log_text)

    def test_pat_without_repository_access_answers_502_auth_failed(self):
        resp, _ = self._get_with_upstream(403, b'{"message":"403 Forbidden"}')

        self.assertEqual(resp.status_code, 502)
        self.assertEqual(json.loads(resp.content)["code"], SCM_AUTH_FAILED_CODE)

    def test_scm_outage_answers_502_unavailable(self):
        for upstream_status in (500, 502, 503, 429):
            with self.subTest(upstream_status=upstream_status):
                resp, _ = self._get_with_upstream(upstream_status, b"<html>oops</html>")

                self.assertEqual(resp.status_code, 502)
                self.assertEqual(
                    json.loads(resp.content),
                    {"detail": ERR_SCM_UNAVAILABLE, "code": SCM_UNAVAILABLE_CODE},
                )

    def test_error_body_exposes_neither_the_token_nor_the_upstream_url(self):
        for upstream_status in (401, 403, 500):
            with self.subTest(upstream_status=upstream_status):
                resp, _ = self._get_with_upstream(upstream_status, b'{"message":"invalid_token"}')

                self.assertBodyLeaksNothing(resp, EXPIRED_GITLAB_PAT, INTERNAL_GITLAB_HOST, "invalid_token")

    def test_missing_file_is_still_404(self):
        resp, _ = self._get_with_upstream(404, b'{"message":"404 File Not Found"}')

        self.assertEqual(resp.status_code, 404)

    def test_file_is_still_served_when_the_token_is_valid(self):
        resp, _ = self._get_with_upstream(200, b"DEBUG = False\n")

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.content, b"DEBUG = False\n")

    @patch("aist.tasks.egress.prewarm_egress.delay")
    def test_cold_vpn_tunnel_still_answers_202_warming(self, mock_prewarm):
        with (
            patch("aist.api.files.egress.proxy_url_for_project_version", return_value="http://egress-proxy:1080"),
            patch("aist.api.files.requests.get", side_effect=requests.ConnectionError("tunnel down")),
        ):
            resp = self.client.get(self._blob_url(self.pv))

        self.assertEqual(resp.status_code, 202)
        self.assertEqual(json.loads(resp.content)["status"], "warming")
        mock_prewarm.assert_called_once_with(self.pv.id)


class GerritRejectedPasswordBlobTests(_ScmBlobBase):

    """Gerrit goes through ``ScmGerritBinding.fetch_raw_bytes``, not ``_return_remote_bytes``."""

    def setUp(self):
        super().setUp()
        integration = OrgIntegration.objects.create(
            organization=self.organization,
            integration_type=OrgIntegrationType.GERRIT,
            name="Corporate Gerrit",
            config={"username": "svc-aist"},
            secret=GERRIT_HTTP_PASSWORD,
            is_active=True,
        )
        repository = RepositoryInfo.objects.create(
            type=ScmType.GERRIT,
            repo_owner="platform",
            repo_name="server",
            base_url=f"https://{INTERNAL_GERRIT_HOST}",
        )
        self.binding = ScmGerritBinding.objects.create(scm=repository, org_integration=integration)
        self.repository = repository
        self.pv = self._git_version(repository)

    def _get_with_upstream(self, status_code: int):
        url = self.binding.build_raw_url(self.repository, COMMIT, "src/main.c")
        with patch("requests.get", return_value=_scm_response(status_code, b"Unauthorized", url)):
            return self.client.get(self._blob_url(self.pv, "src/main.c"))

    def test_rejected_http_password_answers_502_auth_failed_not_404(self):
        # Previously the 401 was swallowed into None and reported as "file not found".
        resp = self._get_with_upstream(401)

        self.assertEqual(resp.status_code, 502)
        self.assertEqual(json.loads(resp.content)["code"], SCM_AUTH_FAILED_CODE)
        self.assertBodyLeaksNothing(resp, GERRIT_HTTP_PASSWORD, INTERNAL_GERRIT_HOST)

    def test_gerrit_outage_answers_502_unavailable(self):
        resp = self._get_with_upstream(503)

        self.assertEqual(resp.status_code, 502)
        self.assertEqual(json.loads(resp.content)["code"], SCM_UNAVAILABLE_CODE)

    def test_gerrit_missing_file_is_still_404(self):
        resp = self._get_with_upstream(404)

        self.assertEqual(resp.status_code, 404)

    def test_fetch_raw_bytes_raises_typed_error_for_every_non_404_failure(self):
        for upstream_status, is_auth in ((401, True), (403, True), (500, False), (503, False)):
            with self.subTest(upstream_status=upstream_status):
                url = self.binding.build_raw_url(self.repository, COMMIT, "src/main.c")
                with (
                    patch("requests.get", return_value=_scm_response(upstream_status, b"", url)),
                    self.assertRaises(ScmFetchError) as ctx,
                ):
                    self.binding.fetch_raw_bytes(self.repository, COMMIT, "src/main.c")

                self.assertEqual(ctx.exception.status_code, upstream_status)
                self.assertEqual(ctx.exception.is_auth_failure, is_auth)
                self.assertNotIn(INTERNAL_GERRIT_HOST, str(ctx.exception))

    def test_fetch_raw_bytes_still_reraises_connection_errors_through_a_vpn_proxy(self):
        with (
            patch("requests.get", side_effect=requests.ConnectionError("tunnel down")),
            self.assertRaises(requests.ConnectionError),
        ):
            self.binding.fetch_raw_bytes(self.repository, COMMIT, "src/main.c", proxy_url="http://egress-proxy:1080")
