from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.test import TestCase, override_settings
from dojo.models import Product, Product_Type, SLA_Configuration

from aist.actions import EmailAction, SlackAction, WriteLogAction
from aist.models import (
    AISTAIResponse,
    AISTLaunchConfigAction,
    AISTPipeline,
    AISTProject,
    AISTProjectLaunchConfig,
    AISTProjectVersion,
    AISTStatus,
    VersionType,
)


@override_settings(DB_KEY="test-secret")
class ActionsTests(TestCase):
    def setUp(self):
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
        self.launch_config = AISTProjectLaunchConfig.objects.create(
            project=self.project,
            name="Preset",
            description="",
            params={"project_version": {"id": self.pv.id}},
            is_default=False,
        )
        self.pipeline = AISTPipeline.objects.create(
            id="pipe-1",
            project=self.project,
            project_version=self.pv,
            status=AISTStatus.WAITING_RESULT_FROM_AI,
        )

    def _create_ai_response(self):
        payload = {
            "results": {
                "true_positives": [
                    {
                        "title": "Test",
                        "impactScore": 10,
                        "falsePositive": False,
                        "originalFinding": {
                            "cwe": 22,
                            "file": "file.py",
                            "line": 1,
                            "snippet": "print('x')",
                        },
                    },
                ],
            },
        }
        return AISTAIResponse.objects.create(pipeline=self.pipeline, payload=payload)

    def _make_action(self, action_type: str, config: dict, secret: dict | None = None):
        action = AISTLaunchConfigAction.objects.create(
            launch_config=self.launch_config,
            trigger_status=AISTStatus.FINISHED,
            action_type=action_type,
            config=config,
        )
        if secret:
            action.set_secret_config(secret)
            action.save(update_fields=["secret_config"])
        return action

    @patch("aist.actions.AISTSlackNotificationManager.send_message_with_file")
    def test_slack_action_sends_file_when_requested(self, mock_send_file):
        self._create_ai_response()
        action = self._make_action(
            AISTLaunchConfigAction.ActionType.PUSH_TO_SLACK,
            {"channels": ["#alerts"], "include_ai_csv": True},
            {"slack_token": "xoxb-test"},
        )
        SlackAction(action).run(pipeline=self.pipeline, new_status=AISTStatus.FINISHED)
        self.assertTrue(mock_send_file.called)
        args = mock_send_file.call_args.kwargs
        self.assertTrue(args.get("file_content"))

    def test_slack_action_fails_without_ai_response_when_csv_requested(self):
        action = self._make_action(
            AISTLaunchConfigAction.ActionType.PUSH_TO_SLACK,
            {"channels": ["#alerts"], "include_ai_csv": True},
            {"slack_token": "xoxb-test"},
        )
        with self.assertRaises(RuntimeError):
            SlackAction(action).run(pipeline=self.pipeline, new_status=AISTStatus.FINISHED)

    @patch("aist.actions.AISTSlackNotificationManager.post_message_with_token")
    def test_slack_action_sends_message_without_csv(self, mock_post):
        action = self._make_action(
            AISTLaunchConfigAction.ActionType.PUSH_TO_SLACK,
            {"channels": ["#alerts"], "include_ai_csv": False},
            {"slack_token": "xoxb-test"},
        )
        SlackAction(action).run(pipeline=self.pipeline, new_status=AISTStatus.FINISHED)
        self.assertTrue(mock_post.called)

    @patch("aist.actions.EmailNotificationManger.send_mail_notification")
    def test_email_action_requires_ai_response_when_csv_requested(self, mock_send):
        action = self._make_action(
            AISTLaunchConfigAction.ActionType.SEND_EMAIL,
            {"emails": ["a@example.com"], "include_ai_csv": True},
        )
        with self.assertRaises(RuntimeError):
            EmailAction(action).run(pipeline=self.pipeline, new_status=AISTStatus.FINISHED)
        self.assertFalse(mock_send.called)

    @patch("aist.actions.EmailNotificationManger.send_mail_notification")
    def test_email_action_sends_without_csv(self, mock_send):
        action = self._make_action(
            AISTLaunchConfigAction.ActionType.SEND_EMAIL,
            {"emails": ["a@example.com"], "include_ai_csv": False},
        )
        EmailAction(action).run(pipeline=self.pipeline, new_status=AISTStatus.FINISHED)
        self.assertTrue(mock_send.called)

    @patch("aist.actions.install_pipeline_logging")
    def test_write_log_action_requires_ai_response_when_csv_requested(self, mock_install):
        mock_install.return_value = SimpleNamespace(info=lambda *_a, **_k: None)
        action = self._make_action(
            AISTLaunchConfigAction.ActionType.WRITE_LOG,
            {"level": "INFO", "include_ai_csv": True},
        )
        with self.assertRaises(RuntimeError):
            WriteLogAction(action).run(pipeline=self.pipeline, new_status=AISTStatus.FINISHED)

    @patch("aist.actions.install_pipeline_logging")
    def test_write_log_action_logs_without_csv(self, mock_install):
        logger = Mock()
        logger.info = Mock()
        mock_install.return_value = logger
        action = self._make_action(
            AISTLaunchConfigAction.ActionType.WRITE_LOG,
            {"level": "INFO", "include_ai_csv": False},
        )
        WriteLogAction(action).run(pipeline=self.pipeline, new_status=AISTStatus.FINISHED)
        self.assertTrue(logger.info.called)
