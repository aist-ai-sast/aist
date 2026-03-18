from __future__ import annotations

import json
from unittest.mock import patch

from django.http import JsonResponse
from django.test import Client, RequestFactory
from django.urls import reverse

from aist.celery_signals import _update_action_run
from aist.logging_transport import get_pipeline_log_path, install_pipeline_logging, uninstall_pipeline_file_logging
from aist.models import AISTPipeline, AISTStatus
from aist.test.test_api import AISTApiBase
from aist.utils.secrets import MASKED_VALUE, mask_sensitive_data, mask_sensitive_text
from aist_site.middleware import AistResponseMaskingMiddleware


class SecretsMaskingUtilsTests(AISTApiBase):
    def test_mask_sensitive_text_masks_generic_secrets_and_url_credentials(self):
        text = "https://user:my-password@example.com/repo.git?api_key=secret-value\npassword=hunter2"

        masked = mask_sensitive_text(text)

        self.assertNotIn("my-password", masked)
        self.assertNotIn("secret-value", masked)
        self.assertNotIn("hunter2", masked)
        self.assertIn(f"https://user:{MASKED_VALUE}@", masked)
        self.assertIn(f"api_key={MASKED_VALUE}", masked)
        self.assertIn(f"password={MASKED_VALUE}", masked)

    def test_mask_sensitive_text_masks_bare_tokens(self):
        text = "gitlab token glpat-abcdef12345678 and github token ghp_0123456789abcdef0123"

        masked = mask_sensitive_text(text)

        self.assertNotIn("glpat-abcdef12345678", masked)
        self.assertNotIn("ghp_0123456789abcdef0123", masked)
        self.assertEqual(masked.count(MASKED_VALUE), 2)

    def test_mask_sensitive_data_masks_only_sensitive_keys(self):
        payload = {
            "title": "Mitigated",
            "cvss": "3.1",
            "line": 33,
            "created": "08/02/2026, 01:00:00",
            "key": "semgrep",
            "token": "plain-token",
        }

        masked = mask_sensitive_data(payload)

        self.assertEqual(masked["title"], "Mitigated")
        self.assertEqual(masked["cvss"], "3.1")
        self.assertEqual(masked["line"], 33)
        self.assertEqual(masked["created"], "08/02/2026, 01:00:00")
        self.assertEqual(masked["key"], "semgrep")
        self.assertEqual(masked["token"], MASKED_VALUE)

    def test_mask_sensitive_data_does_not_mask_work_item_fields(self):
        # external_key, external_id, external_url are issue tracker identifiers, not secrets
        payload = {
            "id": 1,
            "external_key": "PROJ-42",
            "external_id": "1234567",
            "external_url": "https://jira.example.com/browse/PROJ-42",
            "title": "Fix auth bypass",
            "status_category": "OPEN",
        }

        masked = mask_sensitive_data(payload)

        self.assertEqual(masked["external_key"], "PROJ-42")
        self.assertEqual(masked["external_id"], "1234567")
        self.assertEqual(masked["external_url"], "https://jira.example.com/browse/PROJ-42")
        self.assertEqual(masked["title"], "Fix auth bypass")
        self.assertEqual(masked["status_category"], "OPEN")

    def test_mask_sensitive_data_masks_repo_url_credentials_under_non_sensitive_key(self):
        payload = {
            "env": {
                "REPO_URL": "https://oauth2:glpat-example-token-1234567890@gitlab.example.com/dev/example_project.git",
                "PROJECT_NAME": "dev_example_project",
            },
        }

        masked = mask_sensitive_data(payload)
        masked_url = masked["env"]["REPO_URL"]

        self.assertNotIn("glpat-example-token-1234567890", masked_url)
        self.assertIn(f"https://oauth2:{MASKED_VALUE}@", masked_url)
        self.assertEqual(masked["env"]["PROJECT_NAME"], "dev_example_project")

    def test_mask_sensitive_data_masks_github_repo_url_credentials_under_non_sensitive_key(self):
        payload = {
            "env": {
                "REPO_URL": "https://x-access-token:ghp_exampletoken1234567890@github.com/example-org/example-repo.git",
                "PROJECT_NAME": "example-repo",
            },
        }

        masked = mask_sensitive_data(payload)
        masked_url = masked["env"]["REPO_URL"]

        self.assertNotIn("ghp_exampletoken1234567890", masked_url)
        self.assertIn(f"https://x-access-token:{MASKED_VALUE}@", masked_url)
        self.assertEqual(masked["env"]["PROJECT_NAME"], "example-repo")


class PipelineLogsMaskingAPITests(AISTApiBase):
    def setUp(self):
        super().setUp()
        self.pipeline = AISTPipeline.objects.create(
            id="pipe-mask-logs",
            project=self.project,
            project_version=self.pv,
            status=AISTStatus.FINISHED,
        )

    def test_pipeline_logs_full_masks_secrets(self):
        get_pipeline_log_path(self.pipeline.id).write_text("", encoding="utf-8")
        logger = install_pipeline_logging(self.pipeline.id)
        logger.info("clone https://oauth2:glpat-abcdef12345678@gitlab.example.com/group/repo.git")
        logger.info("PRIVATE-TOKEN: glpat-abcdef12345678")
        uninstall_pipeline_file_logging(self.pipeline.id)

        resp = self.client.get(reverse("aist_api:pipeline_logs_full", kwargs={"pipeline_id": self.pipeline.id}))

        self.assertEqual(resp.status_code, 200)
        body = resp.content.decode("utf-8")
        self.assertNotIn("glpat-abcdef12345678", body)
        self.assertIn(f"https://oauth2:{MASKED_VALUE}@", body)
        self.assertIn(f"PRIVATE-TOKEN: {MASKED_VALUE}", body)

    def test_pipeline_logs_progressive_masks_secrets(self):
        get_pipeline_log_path(self.pipeline.id).write_text("", encoding="utf-8")
        logger = install_pipeline_logging(self.pipeline.id)
        logger.info("token=glpat-abcdef12345678")
        uninstall_pipeline_file_logging(self.pipeline.id)

        resp = self.client.get(
            reverse("aist_api:pipeline_logs_progressive", kwargs={"pipeline_id": self.pipeline.id}),
            data={"tail": 10},
        )

        self.assertEqual(resp.status_code, 200)
        body = resp.content.decode("utf-8")
        self.assertNotIn("glpat-abcdef12345678", body)
        self.assertIn(f"token={MASKED_VALUE}", body)

    @patch("aist.utils.secrets.mask_sensitive_text", wraps=mask_sensitive_text)
    def test_pipeline_logs_full_filters_once(self, wrapped_mask):
        log_path = get_pipeline_log_path(self.pipeline.id)
        log_path.write_text("token=plain-token\n", encoding="utf-8")

        resp = self.client.get(reverse("aist_api:pipeline_logs_full", kwargs={"pipeline_id": self.pipeline.id}))

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(wrapped_mask.call_count, 0)

    @patch("aist.utils.secrets.mask_sensitive_text", wraps=mask_sensitive_text)
    def test_pipeline_logs_download_filters_once(self, wrapped_mask):
        log_path = get_pipeline_log_path(self.pipeline.id)
        log_path.write_text("token=plain-token\n", encoding="utf-8")

        resp = self.client.get(reverse("aist_api:pipeline_logs_download", kwargs={"pipeline_id": self.pipeline.id}))

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(wrapped_mask.call_count, 0)

    def test_logging_transport_masks_secrets_before_write(self):
        get_pipeline_log_path(self.pipeline.id).write_text("", encoding="utf-8")
        logger = install_pipeline_logging(self.pipeline.id)
        logger.info("clone https://oauth2:glpat-abcdef12345678@gitlab.example.com/group/repo.git")
        uninstall_pipeline_file_logging(self.pipeline.id)

        content = get_pipeline_log_path(self.pipeline.id).read_text(encoding="utf-8")
        self.assertNotIn("glpat-abcdef12345678", content)
        self.assertIn(f"https://oauth2:{MASKED_VALUE}@", content)


class ActionRunMaskingTests(AISTApiBase):
    def test_update_action_run_masks_error_payload(self):
        pipeline = AISTPipeline.objects.create(
            id="pipe-mask-action",
            project=self.project,
            project_version=self.pv,
            status=AISTStatus.SAST_LAUNCHED,
        )

        _update_action_run(
            pipeline.id,
            key="k1",
            action_type="WRITE_LOG",
            trigger_status=AISTStatus.FINISHED,
            source="one_off",
            status="failed",
            error="failed with PRIVATE-TOKEN: glpat-abcdef12345678",
        )

        pipeline.refresh_from_db()
        runs = (pipeline.launch_data or {}).get("action_runs") or []
        self.assertEqual(len(runs), 1)
        error = runs[0].get("error", "")
        self.assertNotIn("glpat-abcdef12345678", error)
        self.assertIn(f"PRIVATE-TOKEN: {MASKED_VALUE}", error)


class GitlabIntegrationsMaskingTests(AISTApiBase):
    @patch("aist.api.integrations.gitlab.Gitlab")
    def test_gitlab_projects_list_api_masks_exception_details(self, mock_gitlab):
        mock_gitlab.side_effect = Exception(
            "auth failed for https://oauth2:glpat-abcdef12345678@gitlab.example.com",
        )

        resp = self.client.post(
            reverse("aist_api:gitlab_projects_list"),
            data={"gitlab_url": "https://gitlab.example.com", "gitlab_token": "glpat-abcdef12345678"},
            format="json",
        )

        self.assertEqual(resp.status_code, 400)
        self.assertFalse(resp.data["ok"])
        self.assertNotIn("glpat-abcdef12345678", resp.data["error"])
        self.assertIn(MASKED_VALUE, resp.data["error"])


class AistViewsMaskingMiddlewareTests(AISTApiBase):
    def test_masks_json_responses_for_aist_views(self):
        factory = RequestFactory()
        request = factory.get("/aist-admin/aist/projects/gitlab/list/")
        middleware = AistResponseMaskingMiddleware(
            lambda _request: JsonResponse({"token": "plain-token", "ok": True}),
        )

        response = middleware(request)
        payload = json.loads(response.content.decode("utf-8"))

        self.assertEqual(payload.get("token"), MASKED_VALUE)
        self.assertTrue(payload.get("ok"))

    def test_does_not_modify_non_aist_paths(self):
        factory = RequestFactory()
        request = factory.get("/health/")
        middleware = AistResponseMaskingMiddleware(
            lambda _request: JsonResponse({"token": "plain-token", "ok": True}),
        )

        response = middleware(request)
        payload = json.loads(response.content.decode("utf-8"))

        self.assertEqual(payload.get("token"), "plain-token")

    @patch("aist.api.integrations.gitlab.Gitlab")
    def test_masks_real_aist_view_json_response(self, mock_gitlab):
        self.user.is_superuser = True
        self.user.save(update_fields=["is_superuser"])
        client = Client()
        client.force_login(self.user)
        mock_gitlab.side_effect = Exception(
            "auth failed for https://oauth2:glpat-abcdef12345678@gitlab.example.com",
        )

        response = client.post(
            reverse("aist:gitlab_projects_list"),
            data={"gitlab_url": "https://gitlab.example.com", "gitlab_token": "glpat-abcdef12345678"},
        )

        self.assertEqual(response.status_code, 400)
        payload = json.loads(response.content.decode("utf-8"))
        self.assertFalse(payload["ok"])
        self.assertNotIn("glpat-abcdef12345678", payload["error"])
        self.assertIn(MASKED_VALUE, payload["error"])


class PipelineLogsMaskingViewsTests(AISTApiBase):
    def setUp(self):
        super().setUp()
        self.user.is_superuser = True
        self.user.save(update_fields=["is_superuser"])
        self.client = Client()
        self.client.force_login(self.user)
        self.pipeline = AISTPipeline.objects.create(
            id="pipe-mask-views",
            project=self.project,
            project_version=self.pv,
            status=AISTStatus.FINISHED,
        )

    def test_pipeline_logs_full_view_masks_secrets(self):
        get_pipeline_log_path(self.pipeline.id).write_text("", encoding="utf-8")
        logger = install_pipeline_logging(self.pipeline.id)
        logger.info("PRIVATE-TOKEN: secret-value")
        uninstall_pipeline_file_logging(self.pipeline.id)

        resp = self.client.get(reverse("aist:pipeline_logs_full", kwargs={"pipeline_id": self.pipeline.id}))

        self.assertEqual(resp.status_code, 200)
        body = resp.content.decode("utf-8")
        self.assertNotIn("secret-value", body)
        self.assertIn(f"PRIVATE-TOKEN: {MASKED_VALUE}", body)

    @patch("aist.utils.secrets.mask_sensitive_text", wraps=mask_sensitive_text)
    def test_pipeline_logs_full_view_filters_once(self, wrapped_mask):
        log_path = get_pipeline_log_path(self.pipeline.id)
        log_path.write_text("token=plain-token\n", encoding="utf-8")

        resp = self.client.get(reverse("aist:pipeline_logs_full", kwargs={"pipeline_id": self.pipeline.id}))

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(wrapped_mask.call_count, 0)
