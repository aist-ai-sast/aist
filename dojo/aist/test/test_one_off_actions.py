from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from dojo.aist.celery_signals import on_pipeline_status_changed
from dojo.aist.models import AISTPipeline, AISTProject, AISTProjectVersion, AISTStatus, VersionType
from dojo.models import Product, Product_Type, SLA_Configuration


class DummyConfig:
    def get_supported_languages(self):
        return ["python"]

    def get_supported_analyzers(self):
        return ["semgrep"]

    def get_analyzers_time_class(self):
        return ["slow"]

    def get_filtered_analyzers(self, **_kwargs):
        return []

    def get_names(self, _filtered):
        return []


class OneOffActionsTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.user = get_user_model().objects.create_user(
            username="tester",
            email="tester@example.com",
            password="pass",  # noqa: S106
        )
        self.client.force_login(self.user)

        self.sla = SLA_Configuration.objects.create(name="SLA default")
        self.prod_type = Product_Type.objects.create(name="PT")
        self.product = Product.objects.create(
            name="Test Product",
            description="desc",
            prod_type=self.prod_type,
            sla_configuration_id=self.sla.id,
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

    @override_settings(DB_KEY="test-secret")
    @patch("dojo.aist.views.pipelines.run_sast_pipeline")
    def test_start_pipeline_persists_one_off_actions(self, mock_run_task):
        mock_run_task.delay.return_value = SimpleNamespace(id="celery-123")

        with patch("dojo.aist.forms._load_analyzers_config", return_value=DummyConfig()):
            url = reverse("dojo_aist:start_pipeline")
            payload = {
                "project": self.project.id,
                "project_version": self.pv.id,
                "log_level": "INFO",
                "time_class_level": "slow",
                "ai_mode": "MANUAL",
                "one_off_actions": json.dumps(
                    [
                        {
                            "trigger_status": AISTStatus.FINISHED,
                            "action_type": "PUSH_TO_SLACK",
                            "config": {"channels": ["#alerts"], "title": "Hi", "description": "Desc"},
                            "secret_config": {"slack_token": "xoxb-test"},
                        },
                    ],
                ),
            }

            resp = self.client.post(url, data=payload)
            self.assertEqual(resp.status_code, 302)

        pipeline = AISTPipeline.objects.order_by("-created").first()
        self.assertIsNotNone(pipeline)
        launch_data = pipeline.launch_data or {}
        actions = launch_data.get("one_off_actions") or []
        self.assertEqual(len(actions), 1)
        stored = actions[0]
        self.assertEqual(stored["action_type"], "PUSH_TO_SLACK")
        self.assertTrue(stored.get("secret_config"))
        self.assertNotEqual(stored["secret_config"].get("slack_token"), "xoxb-test")

    @patch("dojo.aist.celery_signals.get_action_handler")
    def test_one_off_action_runs_once(self, mock_get_handler):
        class DummyHandler:
            def __init__(self):
                self.calls = 0

            def run(self, **_kwargs):
                self.calls += 1

        handler = DummyHandler()
        mock_get_handler.return_value = handler

        pipeline = AISTPipeline.objects.create(
            id="pipe-1",
            project=self.project,
            project_version=self.pv,
            status=AISTStatus.SAST_LAUNCHED,
            launch_data={
                "one_off_actions": [
                    {
                        "id": "a1",
                        "trigger_status": AISTStatus.FINISHED,
                        "action_type": "WRITE_LOG",
                        "config": {"level": "INFO"},
                        "secret_config": {},
                    },
                ],
                "one_off_actions_done": [],
            },
        )

        on_pipeline_status_changed(
            sender=AISTPipeline,
            pipeline_id=pipeline.id,
            old_status=AISTStatus.SAST_LAUNCHED,
            new_status=AISTStatus.FINISHED,
        )
        on_pipeline_status_changed(
            sender=AISTPipeline,
            pipeline_id=pipeline.id,
            old_status=AISTStatus.SAST_LAUNCHED,
            new_status=AISTStatus.FINISHED,
        )

        pipeline.refresh_from_db()
        done_ids = set(pipeline.launch_data.get("one_off_actions_done") or [])
        self.assertEqual(done_ids, {"a1"})
        self.assertEqual(handler.calls, 1)
