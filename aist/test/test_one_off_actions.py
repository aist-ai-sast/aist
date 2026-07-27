from __future__ import annotations

import json
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.http import QueryDict
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from dojo.authorization.roles_permissions import Roles
from dojo.models import Product, Product_Type, Product_Type_Member, Role, SLA_Configuration

from aist.celery_signals import on_pipeline_status_changed
from aist.forms import AISTPipelineRunForm
from aist.models import (
    AISTPipeline,
    AISTProject,
    AISTProjectVersion,
    AISTStatus,
    Organization,
    PipelineLaunchOrigin,
    PipelineLaunchRequest,
    VersionType,
)


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
        self.organization = Organization.objects.create(
            name="One-off action organization",
            product_type=self.prod_type,
        )
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
    def test_start_pipeline_persists_one_off_actions_in_launch_request(self):
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
                        },
                    ],
                ),
            }

            resp = self.client.post(url, data=payload)
            self.assertEqual(resp.status_code, 302)

        launch_request = PipelineLaunchRequest.objects.get()
        launch_data = launch_request.initial_launch_data_snapshot
        actions = launch_data.get("one_off_actions") or []
        self.assertEqual(len(actions), 1)
        stored = actions[0]
        self.assertEqual(stored["action_type"], "PUSH_TO_SLACK")
        self.assertNotIn("secret_config", stored)
        self.assertFalse(AISTPipeline.objects.exists())

    @patch("celery.app.task.Task.apply_async")
    def test_repeated_start_click_is_idempotent_and_preserves_requester(self, mock_apply_async):
        with patch("aist.forms._load_analyzers_config", return_value=DummyConfig()):
            url = reverse("aist:start_pipeline")
            payload = {
                "project": self.project.id,
                "project_version": self.pv.id,
                "log_level": "INFO",
                "time_class_level": "slow",
                "ai_mode": "MANUAL",
                "one_off_actions": "[]",
                "client_request_key": "classic-start-click-1",
            }

            first = self.client.post(url, data=payload)
            second = self.client.post(url, data=payload)
            self.assertEqual(first.status_code, 302)
            self.assertEqual(second.status_code, 302)

        launch_request = PipelineLaunchRequest.objects.get()
        self.assertEqual(launch_request.requester, self.user)
        self.assertEqual(launch_request.origin, PipelineLaunchOrigin.MANUAL)
        expected_location = f"{reverse('aist:launching_dashboard')}?queued_request={launch_request.pk}"
        self.assertEqual(first.headers.get("Location"), expected_location)
        self.assertEqual(second.headers.get("Location"), expected_location)
        self.assertFalse(AISTPipeline.objects.exists())
        mock_apply_async.assert_not_called()

    def test_start_pipeline_keeps_explicit_analyzers_selection_in_request(self):
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

        launch_request = PipelineLaunchRequest.objects.get()
        self.assertEqual(launch_request.params_snapshot.get("analyzers"), ["bearer"])

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
