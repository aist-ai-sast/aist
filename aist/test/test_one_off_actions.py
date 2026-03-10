from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.http import QueryDict
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from dojo.authorization.roles_permissions import Roles
from dojo.models import Product, Product_Type, Product_Type_Member, Role, SLA_Configuration

from aist.celery_signals import on_pipeline_status_changed
from aist.forms import AISTPipelineRunForm
from aist.models import AISTPipeline, AISTProject, AISTProjectVersion, AISTStatus, VersionType


class DummyConfig:
    def get_supported_languages(self):
        return ["python"]

    def get_supported_analyzers(self):
        return ["semgrep", "snyk", "bearer"]

    def get_analyzers_time_class(self):
        return ["slow"]

    def get_filtered_analyzers(self, **_kwargs):
        return ["semgrep", "snyk", "bearer"]

    def get_names(self, _filtered):
        return list(_filtered)


class OneOffActionsTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.user = get_user_model().objects.create_user(
            username="tester",
            email="tester@example.com",
            password="pass",  # noqa: S106
        )
        self.user.is_superuser = True
        self.user.save(update_fields=["is_superuser"])
        self.client.force_login(self.user)

        self.sla = SLA_Configuration.objects.create(name="SLA default")
        self.prod_type = Product_Type.objects.create(name="PT")
        self.product = Product.objects.create(
            name="Test Product",
            description="desc",
            prod_type=self.prod_type,
            sla_configuration_id=self.sla.id,
        )
        role_maintainer, _ = Role.objects.get_or_create(
            id=Roles.Maintainer,
            defaults={"name": "Maintainer"},
        )
        Product_Type_Member.objects.create(
            product_type=self.prod_type,
            user=self.user,
            role=role_maintainer,
        )

        self.project = AISTProject.objects.create(
            product=self.product,
            supported_languages=["python"],
            compilable=False,
            profile={},
        )

        self.pv = AISTProjectVersion.objects.create(
            project=self.project,
            version_type=VersionType.GIT_HASH,
            version="main",
        )

    @override_settings(DB_KEY="test-secret")
    @patch("aist.views.pipelines.run_sast_pipeline")
    def test_start_pipeline_persists_one_off_actions(self, mock_run_task):
        mock_run_task.delay.return_value = SimpleNamespace(id="celery-123")

        with patch("aist.forms._load_analyzers_config", return_value=DummyConfig()):
            url = reverse("aist:start_pipeline")
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

    @patch("celery.app.task.Task.apply_async")
    def test_start_pipeline_passes_request_user_to_celery_task(self, mock_apply_async):
        mock_apply_async.return_value = SimpleNamespace(id="celery-apply-1")

        with patch("aist.forms._load_analyzers_config", return_value=DummyConfig()):
            url = reverse("aist:start_pipeline")
            payload = {
                "project": self.project.id,
                "project_version": self.pv.id,
                "log_level": "INFO",
                "time_class_level": "slow",
                "ai_mode": "MANUAL",
                "one_off_actions": "[]",
            }

            resp = self.client.post(url, data=payload)
            self.assertEqual(resp.status_code, 302)

        self.assertTrue(mock_apply_async.called)
        task_args = mock_apply_async.call_args.kwargs.get("args", ())
        task_kwargs = mock_apply_async.call_args.kwargs.get("kwargs", {})
        self.assertGreaterEqual(len(task_args), 2)
        self.assertIn("async_user", task_kwargs)
        self.assertEqual(task_kwargs["async_user"], self.user)

        pipeline = AISTPipeline.objects.order_by("-created").first()
        self.assertIsNotNone(pipeline)
        self.assertEqual(pipeline.run_task_id, "celery-apply-1")
        self.assertEqual(str(task_args[0]), str(pipeline.id))

        expected_location = reverse("aist:pipeline_detail", kwargs={"pipeline_id": pipeline.id})
        self.assertEqual(resp.headers.get("Location"), expected_location)

        detail_resp = self.client.get(expected_location)
        self.assertEqual(detail_resp.status_code, 200)

    @patch("celery.app.task.Task.apply_async")
    def test_start_pipeline_keeps_explicit_analyzers_selection(self, mock_apply_async):
        mock_apply_async.return_value = SimpleNamespace(id="celery-apply-analyzers")

        with patch("aist.forms._load_analyzers_config", return_value=DummyConfig()):
            url = reverse("aist:start_pipeline")
            payload = {
                "project": self.project.id,
                "project_version": self.pv.id,
                "log_level": "INFO",
                "time_class_level": "slow",
                "analyzers": ["bearer"],
                "ai_mode": "MANUAL",
                "one_off_actions": "[]",
            }
            resp = self.client.post(url, data=payload)
            self.assertEqual(resp.status_code, 302)

        task_args = mock_apply_async.call_args.kwargs.get("args", ())
        self.assertGreaterEqual(len(task_args), 2)
        params = task_args[1]
        self.assertEqual(params.get("analyzers"), ["bearer"])

    def test_start_pipeline_recomputes_default_analyzers_when_language_changed(self):
        with patch("aist.forms._load_analyzers_config", return_value=DummyConfig()):
            query_data = QueryDict("", mutable=True)
            query_data.update(
                {
                    "project": str(self.project.id),
                    "time_class_level": "slow",
                },
            )
            query_data.setlist("languages", ["go"])
            query_data["selection_signature"] = "stale-signature"

            form = AISTPipelineRunForm(query_data)
            self.assertEqual(form.data.getlist("analyzers"), ["semgrep", "snyk", "bearer"])

    @patch("aist.celery_signals.get_action_handler")
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
