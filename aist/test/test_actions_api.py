from __future__ import annotations

from django.urls import reverse

from aist.models import AISTLaunchConfigAction, AISTProjectLaunchConfig, AISTStatus
from aist.test.test_api import AISTApiBase
from aist.utils.secrets import MASKED_VALUE


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
            "aist_api:project_launch_config_action_list_create",
            kwargs={"project_id": self.project.id, "config_id": cfg_id},
        )

    def _action_detail_url(self, cfg_id: int, action_id: int):
        return reverse(
            "aist_api:project_launch_config_action_detail",
            kwargs={"project_id": self.project.id, "config_id": cfg_id, "action_id": action_id},
        )

    def test_slack_action_requires_channels(self):
        cfg = self._config()
        resp = self.client.post(
            self._actions_url(cfg.id),
            data={
                "trigger_status": AISTStatus.FINISHED,
                "action_type": "PUSH_TO_SLACK",
                "config": {"title": "Hi"},
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

    def test_slack_action_created_without_token_field(self):
        cfg = self._config()
        resp = self.client.post(
            self._actions_url(cfg.id),
            data={
                "trigger_status": AISTStatus.FINISHED,
                "action_type": "PUSH_TO_SLACK",
                "config": {"channels": ["#alerts"]},
            },
            format="json",
        )
        self.assertEqual(resp.status_code, 201)
        self.assertNotIn("secret_config", resp.data)

    def test_include_ai_csv_flag_persists(self):
        cfg = self._config()
        resp = self.client.post(
            self._actions_url(cfg.id),
            data={
                "trigger_status": AISTStatus.FINISHED,
                "action_type": "PUSH_TO_SLACK",
                "config": {"channels": ["#alerts"], "include_ai_csv": True},
            },
            format="json",
        )
        self.assertEqual(resp.status_code, 201)
        self.assertTrue(resp.data.get("config", {}).get("include_ai_csv"))

    def test_slack_action_rejects_both_common_and_ai_summary(self):
        cfg = self._config()
        resp = self.client.post(
            self._actions_url(cfg.id),
            data={
                "trigger_status": AISTStatus.FINISHED,
                "action_type": "PUSH_TO_SLACK",
                "config": {
                    "channels": ["#alerts"],
                    "include_ai_csv": True,
                    "include_common_summary": True,
                },
            },
            format="json",
        )
        self.assertEqual(resp.status_code, 400)
        self.assertIn("config", resp.data)

    def test_email_action_rejects_both_common_and_ai_summary(self):
        cfg = self._config()
        resp = self.client.post(
            self._actions_url(cfg.id),
            data={
                "trigger_status": AISTStatus.FINISHED,
                "action_type": "SEND_EMAIL",
                "config": {
                    "emails": ["a@example.com"],
                    "include_ai_csv": True,
                    "include_common_summary": True,
                },
            },
            format="json",
        )
        self.assertEqual(resp.status_code, 400)
        self.assertIn("config", resp.data)

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

    def test_update_action_config(self):
        cfg = self._config()
        create_resp = self.client.post(
            self._actions_url(cfg.id),
            data={
                "trigger_status": AISTStatus.FINISHED,
                "action_type": "WRITE_LOG",
                "config": {"level": "INFO"},
            },
            format="json",
        )
        self.assertEqual(create_resp.status_code, 201)
        action_id = create_resp.data["id"]

        resp = self.client.patch(
            self._action_detail_url(cfg.id, action_id),
            data={"config": {"level": "WARNING"}},
            format="json",
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data["config"]["level"], "WARNING")

    def test_action_detail_masks_sensitive_config_fields(self):
        cfg = self._config()
        action = AISTLaunchConfigAction.objects.create(
            launch_config=cfg,
            trigger_status=AISTStatus.FINISHED,
            action_type=AISTLaunchConfigAction.ActionType.WRITE_LOG,
            config={"token": "plain-token", "level": "INFO"},
        )

        detail_resp = self.client.get(self._action_detail_url(cfg.id, action.id))
        self.assertEqual(detail_resp.status_code, 200)
        self.assertEqual(detail_resp.data.get("config", {}).get("token"), MASKED_VALUE)
        self.assertEqual(detail_resp.data.get("config", {}).get("level"), "INFO")

    def test_duplicate_actions_allowed(self):
        cfg = self._config()
        first = self.client.post(
            self._actions_url(cfg.id),
            data={
                "trigger_status": AISTStatus.FINISHED,
                "action_type": "WRITE_LOG",
                "config": {"level": "INFO"},
            },
            format="json",
        )
        self.assertEqual(first.status_code, 201)

        second = self.client.post(
            self._actions_url(cfg.id),
            data={
                "trigger_status": AISTStatus.FINISHED,
                "action_type": "WRITE_LOG",
                "config": {"level": "INFO"},
            },
            format="json",
        )
        self.assertEqual(second.status_code, 201)

    def test_delete_action(self):
        cfg = self._config()
        create_resp = self.client.post(
            self._actions_url(cfg.id),
            data={
                "trigger_status": AISTStatus.FINISHED,
                "action_type": "WRITE_LOG",
                "config": {"level": "INFO"},
            },
            format="json",
        )
        self.assertEqual(create_resp.status_code, 201)
        action_id = create_resp.data["id"]

        resp = self.client.delete(self._action_detail_url(cfg.id, action_id))
        self.assertEqual(resp.status_code, 204)
