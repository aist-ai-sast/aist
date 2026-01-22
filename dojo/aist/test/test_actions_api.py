from __future__ import annotations

from django.urls import reverse

from dojo.aist.models import AISTProjectLaunchConfig, AISTStatus
from dojo.aist.test.test_api import AISTApiBase


class LaunchConfigActionsAPITests(AISTApiBase):
    def _config(self):
        return AISTProjectLaunchConfig.objects.create(
            project=self.project,
            name="Preset",
            description="",
            params={"project_version": {"id": self.pv.id}},
            is_default=False,
        )

    def _actions_url(self, cfg_id: int):
        return reverse(
            "dojo_aist_api:project_launch_config_action_list_create",
            kwargs={"project_id": self.project.id, "config_id": cfg_id},
        )

    def test_slack_action_requires_channels(self):
        cfg = self._config()
        resp = self.client.post(
            self._actions_url(cfg.id),
            data={
                "trigger_status": AISTStatus.FINISHED,
                "action_type": "PUSH_TO_SLACK",
                "config": {"title": "Hi"},
                "secret_config": {"slack_token": "xoxb-test"},
            },
            format="json",
        )
        self.assertEqual(resp.status_code, 400)
        self.assertIn("config", resp.data)

    def test_email_action_requires_emails(self):
        cfg = self._config()
        resp = self.client.post(
            self._actions_url(cfg.id),
            data={
                "trigger_status": AISTStatus.FINISHED,
                "action_type": "SEND_EMAIL",
                "config": {"title": "Hi"},
            },
            format="json",
        )
        self.assertEqual(resp.status_code, 400)
        self.assertIn("config", resp.data)

    def test_write_log_action_accepts_defaults(self):
        cfg = self._config()
        resp = self.client.post(
            self._actions_url(cfg.id),
            data={
                "trigger_status": AISTStatus.FINISHED,
                "action_type": "WRITE_LOG",
                "config": {},
            },
            format="json",
        )
        self.assertEqual(resp.status_code, 201)

    def test_secret_config_not_returned(self):
        cfg = self._config()
        resp = self.client.post(
            self._actions_url(cfg.id),
            data={
                "trigger_status": AISTStatus.FINISHED,
                "action_type": "PUSH_TO_SLACK",
                "config": {"channels": ["#alerts"]},
                "secret_config": {"slack_token": "xoxb-test"},
            },
            format="json",
        )
        self.assertEqual(resp.status_code, 201)
        self.assertNotIn("secret_config", resp.data)

    def test_action_type_mismatch_rejected(self):
        cfg = self._config()
        resp = self.client.post(
            self._actions_url(cfg.id),
            data={
                "trigger_status": AISTStatus.FINISHED,
                "action_type": "SEND_EMAIL",
                "config": {"channels": ["#alerts"]},
            },
            format="json",
        )
        self.assertEqual(resp.status_code, 400)
        self.assertIn("config", resp.data)
