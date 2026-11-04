"""
Snippet loading when the SCM refuses or fails the file fetch.

Production scenario: the organization's GitLab PAT expired, GitLab answered every
``/repository/files/.../raw`` request with ``401 {"message":"invalid_token"}`` and the
blob endpoint turned that into a bare 500, so no code snippet loaded anywhere.
"""
from __future__ import annotations

import base64
import json
from unittest.mock import patch

import requests

from aist.api.files import (
    ERR_SCM_AUTH_FAILED,
    ERR_SCM_UNAVAILABLE,
    SCM_AUTH_FAILED_CODE,
    SCM_UNAVAILABLE_CODE,
)
from aist.integrations.scm_errors import ScmFetchError
from aist.models import (
    Organization,
    OrgIntegration,
    OrgIntegrationType,
    RepositoryInfo,
    ScmGitlabBinding,
    ScmType,
)
from aist.test.source_version_fixtures import COMMIT, GERRIT_HTTP_PASSWORD, FakeScmHost, SourceVersionMixin
from aist.test.test_api import AISTApiBase

EXPIRED_GITLAB_PAT = "xglpat-expiredTOKEN1234567890".removeprefix("x")
INTERNAL_GITLAB_HOST = "gitlab.internal.example"
INTERNAL_GERRIT_HOST = "gerrit.internal.example"
SUBPATH = "cloud/cloud/settings.py"


class _ScmBlobBase(SourceVersionMixin, AISTApiBase):
    def assertBodyLeaksNothing(self, resp, *secrets: str):
        body = resp.content.decode("utf-8")
        for fragment in (*secrets, COMMIT, "/api/v4/", "?ref=", "/a/projects/", "http://", "https://"):
            self.assertNotIn(fragment, body)


class GitlabExpiredTokenBlobTests(_ScmBlobBase):
    def setUp(self):
        super().setUp()
        organization = Organization.objects.create(name="SCM Org", product_type=self.prod_type)
        integration = OrgIntegration.objects.create(
            organization=organization,
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
        self.pv = self.repository_version(repository)
        self.upstream_url = (
            f"https://{INTERNAL_GITLAB_HOST}/api/v4/projects/dev%2Fcloud_portal/repository/files/"
            f"cloud%2Fcloud%2Fsettings.py/raw?ref={COMMIT}"
        )

    def _get_with_upstream(self, status_code: int, body: bytes):
        with FakeScmHost(body=body, status_code=status_code) as host:
            resp = self.client.get(self.blob_url(self.pv, SUBPATH))
        return resp, host

    def test_expired_pat_answers_502_with_an_actionable_message_instead_of_500(self):
        with self.assertLogs("aist.api.files", level="WARNING") as logs:
            resp, host = self._get_with_upstream(401, b'{"message":"invalid_token"}')

        # The PAT was actually sent upstream: the failure is GitLab rejecting it.
        self.assertEqual(host.last.url, self.upstream_url)
        self.assertEqual(host.last.headers["PRIVATE-TOKEN"], EXPIRED_GITLAB_PAT)
        self.assertEqual(resp.status_code, 502)
        payload = json.loads(resp.content)
        self.assertEqual(payload, {"detail": ERR_SCM_AUTH_FAILED, "code": SCM_AUTH_FAILED_CODE})

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

    def test_scm_outage_answers_502_unavailable_without_retrying(self):
        for upstream_status in (500, 502, 503, 429):
            with self.subTest(upstream_status=upstream_status):
                resp, host = self._get_with_upstream(upstream_status, b"<html>oops</html>")

                self.assertEqual(resp.status_code, 502)
                self.assertEqual(
                    json.loads(resp.content),
                    {"detail": ERR_SCM_UNAVAILABLE, "code": SCM_UNAVAILABLE_CODE},
                )
                self.assertEqual(len(host.requests), 1)

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
        self.assertEqual(b"".join(resp.streaming_content), b"DEBUG = False\n")

    @patch("aist.tasks.egress.prewarm_egress.delay")
    def test_cold_vpn_tunnel_still_answers_202_warming(self, mock_prewarm):
        with (
            patch("aist.api.files.egress.proxy_url_for_project_version", return_value="http://egress-proxy:1080"),
            FakeScmHost(error=requests.ConnectionError("tunnel down")),
        ):
            resp = self.client.get(self.blob_url(self.pv, SUBPATH))

        self.assertEqual(resp.status_code, 202)
        self.assertEqual(json.loads(resp.content)["status"], "warming")
        mock_prewarm.assert_called_once_with(self.pv.id)


class GerritRejectedPasswordBlobTests(_ScmBlobBase):
    def setUp(self):
        super().setUp()
        self.repository, _binding = self.gerrit_repository(INTERNAL_GERRIT_HOST)
        self.pv = self.repository_version(self.repository)

    def _get_with_upstream(self, status_code: int, body: bytes = b"Unauthorized"):
        with FakeScmHost(body=body, status_code=status_code):
            return self.client.get(self.blob_url(self.pv, "src/main.c"))

    def test_decoded_file_is_served_with_the_http_password(self):
        with FakeScmHost(body=base64.b64encode(b"int main(void);\n")) as host:
            resp = self.client.get(self.blob_url(self.pv, "src/main.c"))

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(b"".join(resp.streaming_content), b"int main(void);\n")
        self.assertEqual(
            host.last.url,
            f"https://{INTERNAL_GERRIT_HOST}/a/projects/platform%2Fserver/branches/{COMMIT}/files/src%2Fmain.c/content",
        )
        expected = base64.b64encode(f"svc-aist:{GERRIT_HTTP_PASSWORD}".encode()).decode()
        self.assertEqual(host.last.headers["Authorization"], f"Basic {expected}")

    def test_rejected_http_password_answers_502_auth_failed_not_404(self):
        resp = self._get_with_upstream(401)

        self.assertEqual(resp.status_code, 502)
        self.assertEqual(json.loads(resp.content)["code"], SCM_AUTH_FAILED_CODE)
        self.assertBodyLeaksNothing(resp, GERRIT_HTTP_PASSWORD, INTERNAL_GERRIT_HOST)

    def test_gerrit_outage_and_undecodable_body_answer_502_unavailable(self):
        for status_code, body in ((503, b"Unauthorized"), (200, b"not base64 at all!")):
            with self.subTest(status_code=status_code):
                resp = self._get_with_upstream(status_code, body)

                self.assertEqual(resp.status_code, 502)
                self.assertEqual(json.loads(resp.content)["code"], SCM_UNAVAILABLE_CODE)

    def test_gerrit_missing_file_is_still_404(self):
        resp = self._get_with_upstream(404)

        self.assertEqual(resp.status_code, 404)

    def test_every_non_404_failure_raises_a_typed_error_without_the_host(self):
        for upstream_status, is_auth in ((401, True), (403, True), (500, False), (503, False)):
            with (
                self.subTest(upstream_status=upstream_status),
                FakeScmHost(status_code=upstream_status),
                self.assertRaises(ScmFetchError) as ctx,
            ):
                self.repository.read_file(COMMIT, "src/main.c")

            self.assertEqual(ctx.exception.status_code, upstream_status)
            self.assertEqual(ctx.exception.is_auth_failure, is_auth)
            self.assertNotIn(INTERNAL_GERRIT_HOST, str(ctx.exception))

    def test_connection_errors_propagate_with_and_without_a_vpn_proxy(self):
        for proxy_url in ("http://egress-proxy:1080", None):
            with (
                self.subTest(proxy_url=proxy_url),
                FakeScmHost(error=requests.ConnectionError("tunnel down")),
                self.assertRaises(requests.ConnectionError),
            ):
                self.repository.read_file(COMMIT, "src/main.c", proxy_url=proxy_url)
