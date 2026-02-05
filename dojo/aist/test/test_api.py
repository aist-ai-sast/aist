# dojo/aist/test/test_api.py
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from rest_framework.test import APIClient

from dojo.aist.models import AISTPipeline, AISTProject, AISTProjectLaunchConfig, AISTProjectVersion, AISTStatus, VersionType
from dojo.authorization.roles_permissions import Roles
from dojo.models import Product, Product_Member, Product_Type, Role, SLA_Configuration


class AISTApiBase(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = get_user_model().objects.create_user(
            username="tester",
            email="tester@example.com",
            password="pass",  # noqa: S106
        )
        self.client.force_authenticate(user=self.user)

        self.sla = SLA_Configuration.objects.create(name="SLA default")
        self.prod_type = Product_Type.objects.create(name="PT")
        self.role_maintainer, _ = Role.objects.get_or_create(
            id=Roles.Maintainer,
            defaults={"name": "Maintainer"},
        )
        self.product = Product.objects.create(
            name="Test Product",
            description="desc",
            prod_type=self.prod_type,
            sla_configuration_id=self.sla.id,
        )
        Product_Member.objects.create(
            product=self.product,
            user=self.user,
            role=self.role_maintainer,
        )

        self.project = AISTProject.objects.create(
            product=self.product,
            supported_languages=["python"],
            script_path="scripts/build.sh",
            compilable=False,
            profile={},
        )

        self.pv = AISTProjectVersion.objects.create(
            project=self.project,
            version_type=VersionType.GIT_HASH,
            version="main",
        )

        self.other_product = Product.objects.create(
            name="Other Product",
            description="desc",
            prod_type=self.prod_type,
            sla_configuration_id=self.sla.id,
        )
        self.other_project = AISTProject.objects.create(
            product=self.other_product,
            supported_languages=["python"],
            script_path="scripts/build.sh",
            compilable=False,
            profile={},
        )
        self.other_pv = AISTProjectVersion.objects.create(
            project=self.other_project,
            version_type=VersionType.GIT_HASH,
            version="other",
        )


class PipelineStartAPITests(AISTApiBase):
    def _url(self):
        # api_urls.py: path("pipelines/start/", ...)
        return reverse("dojo_aist_api:pipeline_start")

    @patch("dojo.aist.api.pipelines.run_sast_pipeline")
    @patch("dojo.aist.api.pipelines.PipelineArguments.normalize_params")
    def test_start_pipeline_happy_path_calls_celery_with_params(
            self, mock_normalize, mock_run_task,
    ):
        mock_normalize.return_value = {
            "project_id": self.project.id,
            "project_version": {"id": self.pv.id},
            "log_level": "INFO",
        }
        mock_run_task.delay.return_value = SimpleNamespace(id="celery-123")

        resp = self.client.post(
            self._url(),
            data={
                "project_version_id": self.pv.id,
                "ai_filter": {
                    "limit": 50,
                    "severity": [{"comparison": "EQUALS", "value": "HIGH"}],
                },
            },
            format="json",
        )

        self.assertEqual(resp.status_code, 201)

        pipeline_id = resp.data["id"]

        mock_run_task.delay.assert_called_once_with(
            pipeline_id,
            mock_normalize.return_value,
        )

    def test_start_pipeline_returns_400_if_filter_required_and_missing(self):
        resp = self.client.post(
            self._url(),
            data={"project_version_id": self.pv.id},
            format="json",
        )

        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.data, {"ai_filter": "ai_filter is required for AUTO_DEFAULT"})


class AISTAuthorizationTests(AISTApiBase):
    def test_project_list_filters_to_authorized_products(self):
        resp = self.client.get(reverse("dojo_aist_api:project_list"))
        self.assertEqual(resp.status_code, 200)
        rows = resp.data.get("results", resp.data)
        ids = {row["id"] for row in rows}
        self.assertIn(self.project.id, ids)
        self.assertNotIn(self.other_project.id, ids)

    def test_project_detail_denies_other_product(self):
        resp = self.client.get(
            reverse("dojo_aist_api:project_detail", kwargs={"project_id": self.other_project.id}),
        )
        self.assertEqual(resp.status_code, 404)

    def test_pipeline_list_filters_to_authorized_products(self):
        own = AISTPipeline.objects.create(
            id="pipe-own",
            project=self.project,
            status=AISTStatus.FINISHED,
        )
        AISTPipeline.objects.create(
            id="pipe-other",
            project=self.other_project,
            status=AISTStatus.FINISHED,
        )

        resp = self.client.get(reverse("dojo_aist_api:pipelines"))
        self.assertEqual(resp.status_code, 200)
        results = resp.data.get("results", [])
        ids = {row["id"] for row in results}
        self.assertIn(own.id, ids)
        self.assertNotIn("pipe-other", ids)

    def test_pipeline_detail_denies_other_product(self):
        AISTPipeline.objects.create(
            id="pipe-other",
            project=self.other_project,
            status=AISTStatus.FINISHED,
        )
        resp = self.client.get(reverse("dojo_aist_api:pipeline_status", kwargs={"pipeline_id": "pipe-other"}))
        self.assertEqual(resp.status_code, 404)


class AISTUIApiTests(AISTApiBase):
    def test_project_update_api(self):
        url = reverse("dojo_aist_api:project_update", kwargs={"project_id": self.project.id})
        resp = self.client.post(
            url,
            data={
                "script_path": "scripts/new.sh",
                "supported_languages": "python, go",
                "profile": "{\"paths\": {\"exclude\": [\"vendor/\"]}}",
            },
        )
        self.assertEqual(resp.status_code, 200)
        self.project.refresh_from_db()
        self.assertEqual(self.project.script_path, "scripts/new.sh")

    def test_pipeline_stop_api(self):
        pipeline = AISTPipeline.objects.create(
            id="pipe-stop-1",
            project=self.project,
            project_version=self.pv,
            status=AISTStatus.SAST_LAUNCHED,
            run_task_id="celery-1",
        )
        url = reverse("dojo_aist_api:pipeline_stop", kwargs={"pipeline_id": pipeline.id})
        resp = self.client.post(url)
        self.assertEqual(resp.status_code, 200)
        pipeline.refresh_from_db()
        self.assertEqual(pipeline.status, AISTStatus.FINISHED)

    def test_send_request_to_ai_api_requires_waiting_status(self):
        pipeline = AISTPipeline.objects.create(
            id="pipe-ai-1",
            project=self.project,
            project_version=self.pv,
            status=AISTStatus.FINISHED,
        )
        url = reverse("dojo_aist_api:pipeline_send_request", kwargs={"pipeline_id": pipeline.id})
        resp = self.client.post(url, data={"finding_ids": []}, format="json")
        self.assertEqual(resp.status_code, 400)

class LaunchConfigAPITests(AISTApiBase):
    def _list_create_url(self):
        return reverse("dojo_aist_api:project_launch_config_list_create", kwargs={"project_id": self.project.id})

    def _detail_url(self, cfg_id: int):
        return reverse(
            "dojo_aist_api:project_launch_config_detail",
            kwargs={"project_id": self.project.id, "config_id": cfg_id},
        )

    def _start_url(self, cfg_id: int):
        # api_urls.py: .../start/
        return reverse(
            "dojo_aist_api:project_launch_config_start",
            kwargs={"project_id": self.project.id, "config_id": cfg_id},
        )

    def test_delete_launch_config(self):
        cfg = AISTProjectLaunchConfig.objects.create(
            project=self.project,
            name="Preset",
            description="",
            params={"project_version": {"id": self.pv.id}},
            is_default=False,
        )
        url = self._detail_url(cfg.id)
        resp = self.client.delete(url)
        self.assertEqual(resp.status_code, 204)
        self.assertFalse(AISTProjectLaunchConfig.objects.filter(id=cfg.id).exists())

    @patch("dojo.aist.api.launch_configs.PipelineArguments.normalize_params")
    def test_update_launch_config_params(self, mock_normalize):
        cfg = AISTProjectLaunchConfig.objects.create(
            project=self.project,
            name="Preset",
            description="",
            params={"project_version": {"id": self.pv.id}},
            is_default=False,
        )
        mock_normalize.return_value = {
            "project_version": {"id": self.pv.id},
            "ai_mode": "AUTO_DEFAULT",
        }

        resp = self.client.patch(
            self._detail_url(cfg.id),
            data={"params": {"ai_mode": "AUTO_DEFAULT"}},
            format="json",
        )

        self.assertEqual(resp.status_code, 200)
        cfg.refresh_from_db()
        self.assertEqual(cfg.params, mock_normalize.return_value)
        mock_normalize.assert_called_once_with(project=self.project, raw_params={"ai_mode": "AUTO_DEFAULT"})

    @patch("dojo.aist.api.launch_configs.PipelineArguments.normalize_params")
    def test_create_launch_config_normalizes_and_strips_project_fields(self, mock_normalize):
        mock_normalize.return_value = {
            "project_id": self.project.id,
            "project_version": {"id": self.pv.id},
            "log_level": "INFO",
            "ai_mode": "AUTO_DEFAULT",
        }

        resp = self.client.post(
            self._list_create_url(),
            data={
                "name": "My preset",
                "is_default": True,
                "params": {"log_level": "INFO"},
            },
            format="json",
        )

        self.assertEqual(resp.status_code, 201)

        cfg = AISTProjectLaunchConfig.objects.get(id=resp.data["id"])

        self.assertEqual(cfg.params["log_level"], "INFO")
        self.assertIn("project_version", cfg.params)

    @patch("dojo.aist.api.launch_configs.PipelineArguments.normalize_params")
    def test_create_default_launch_config_unsets_previous_default(self, mock_normalize):
        mock_normalize.return_value = {"log_level": "INFO"}

        # create first default
        r1 = self.client.post(
            self._list_create_url(),
            data={"name": "Preset 1", "is_default": True, "params": {"log_level": "INFO"}},
            format="json",
        )
        self.assertEqual(r1.status_code, 201)
        cfg1_id = r1.data["id"]

        # create second default -> first should be unset :contentReference[oaicite:8]{index=8}
        r2 = self.client.post(
            self._list_create_url(),
            data={"name": "Preset 2", "is_default": True, "params": {"log_level": "INFO"}},
            format="json",
        )
        self.assertEqual(r2.status_code, 201)
        cfg2_id = r2.data["id"]

        cfg1 = AISTProjectLaunchConfig.objects.get(id=cfg1_id)
        cfg2 = AISTProjectLaunchConfig.objects.get(id=cfg2_id)

        self.assertFalse(cfg1.is_default)
        self.assertTrue(cfg2.is_default)

    @patch("dojo.aist.api.launch_configs.run_sast_pipeline")
    @patch("dojo.aist.api.launch_configs.PipelineArguments.normalize_params")
    @patch("dojo.aist.api.launch_configs.has_unfinished_pipeline", return_value=False)
    def test_start_by_launch_config_uses_latest_version_and_merges_overrides(
            self,
            mock_has_unfinished,
            mock_normalize,
            mock_run_task,
    ):
        # Create a "latest" version that should be chosen when project_version_id omitted :contentReference[oaicite:9]{index=9}
        AISTProjectVersion.objects.create(
            project=self.project,
            version_type=VersionType.GIT_HASH,
            version="develop",
        )

        cfg = AISTProjectLaunchConfig.objects.create(
            project=self.project,
            name="Preset",
            description="",
            params={"log_level": "INFO", "rebuild_images": False},
            is_default=False,
        )

        mock_normalize.return_value = {"project_version": {"id": self.pv.id}}
        mock_run_task.delay.return_value = SimpleNamespace(id="celery-999")

        resp = self.client.post(
            self._start_url(cfg.id),
            data={"params": {"rebuild_images": True}},
            format="json",
        )
        self.assertEqual(resp.status_code, 201)
        mock_has_unfinished.assert_called_once()

        # Ensure normalize got merged raw_params
        _, kwargs = mock_normalize.call_args
        self.assertEqual(kwargs["project"], self.project)
        self.assertEqual(kwargs["raw_params"]["log_level"], "INFO")
        self.assertEqual(kwargs["raw_params"]["rebuild_images"], True)

        mock_run_task.delay.assert_called_once()
