from __future__ import annotations

import tempfile
from pathlib import Path

from django.contrib.auth import get_user_model
from django.urls import reverse
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient

from aist.models import AISTPipeline, AISTStatus
from aist.test.test_api import AISTApiBase

_PROJ_PATH = "/tmp/aist/projects/testproj/main/runs"  # noqa: S108
_STUB_PATH = "/tmp/aist/projects/x/y/runs"  # noqa: S108


class PipelineSourceInfoAPITests(AISTApiBase):

    """Tests for GET /pipelines/<pipeline_id>/source-info/ (internal endpoint)."""

    def setUp(self):
        super().setUp()
        self.service_user = get_user_model().objects.create_superuser(
            username="service_mcp",
            email="service_mcp@example.com",
            password="pass",  # noqa: S106
        )
        self.token = Token.objects.create(user=self.service_user)
        self.token_client = APIClient()
        self.token_client.credentials(HTTP_AUTHORIZATION=f"Token {self.token.key}")

    def _url(self, pipeline_id: str) -> str:
        return reverse("aist_api:pipeline_source_info", kwargs={"pipeline_id": pipeline_id})

    def test_returns_source_info_for_active_pipeline(self):
        project_path = f"{_PROJ_PATH}/pipe-src-info-1"
        pipeline = AISTPipeline.objects.create(
            id="pipe-src-info-1",
            project=self.project,
            project_version=self.pv,
            status=AISTStatus.WAITING_RESULT_FROM_AI,
            launch_data={
                "project_path": project_path,
                "languages": ["python", "javascript"],
            },
        )

        resp = self.token_client.get(self._url(pipeline.id))

        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["pipeline_id"], "pipe-src-info-1")
        self.assertEqual(data["status"], AISTStatus.WAITING_RESULT_FROM_AI)
        self.assertEqual(data["project_path"], project_path)
        self.assertEqual(data["project_name"], "Test Product")
        self.assertEqual(data["languages"], ["python", "javascript"])

    def test_a_checkout_with_a_directory_named_like_the_product_reports_the_scanned_root(self):
        with tempfile.TemporaryDirectory() as checkout:
            (Path(checkout) / self.project.product.name).mkdir()
            pipeline = AISTPipeline.objects.create(
                id="pipe-src-git",
                project=self.project,
                project_version=self.pv,
                status=AISTStatus.WAITING_RESULT_FROM_AI,
                launch_data={"project_path": checkout, "languages": ["python"]},
            )

            resp = self.token_client.get(self._url(pipeline.id))

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["project_path"], checkout)

    def test_returns_409_for_terminal_pipeline(self):
        AISTPipeline.objects.create(
            id="pipe-src-done",
            project=self.project,
            project_version=self.pv,
            status=AISTStatus.FINISHED,
            launch_data={"project_path": f"{_STUB_PATH}/pipe-src-done"},
        )

        resp = self.token_client.get(self._url("pipe-src-done"))

        self.assertEqual(resp.status_code, 409)
        self.assertIn("terminal", resp.json()["detail"])

    def test_returns_409_when_project_path_not_yet_available(self):
        AISTPipeline.objects.create(
            id="pipe-src-nopath",
            project=self.project,
            project_version=self.pv,
            status=AISTStatus.EXECUTING,
            launch_data={},
        )

        resp = self.token_client.get(self._url("pipe-src-nopath"))

        self.assertEqual(resp.status_code, 409)
        self.assertIn("not yet available", resp.json()["detail"])

    def test_returns_404_for_nonexistent_pipeline(self):
        resp = self.token_client.get(self._url("nonexistent-id"))
        self.assertEqual(resp.status_code, 404)

    def test_rejects_unauthenticated_request(self):
        AISTPipeline.objects.create(
            id="pipe-src-noauth",
            project=self.project,
            project_version=self.pv,
            status=AISTStatus.WAITING_RESULT_FROM_AI,
            launch_data={"project_path": f"{_STUB_PATH}/pipe-src-noauth"},
        )

        anon_client = APIClient()
        resp = anon_client.get(self._url("pipe-src-noauth"))

        self.assertEqual(resp.status_code, 403)

    def test_rejects_ordinary_users_even_with_stock_api_token(self):
        ordinary_user = get_user_model().objects.create_user(
            username="ordinary_source_client",
            email="ordinary_source_client@example.com",
            password="pass",  # noqa: S106
        )
        token = Token.objects.create(user=ordinary_user)
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f"Token {token.key}")

        resp = client.get(self._url("nonexistent-id"))

        self.assertEqual(resp.status_code, 403)

    def test_returns_409_for_finished_with_warnings(self):
        AISTPipeline.objects.create(
            id="pipe-src-warn",
            project=self.project,
            project_version=self.pv,
            status=AISTStatus.FINISHED_WITH_WARNINGS,
            launch_data={"project_path": f"{_STUB_PATH}/pipe-src-warn"},
        )

        resp = self.token_client.get(self._url("pipe-src-warn"))

        self.assertEqual(resp.status_code, 409)
