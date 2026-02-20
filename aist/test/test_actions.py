from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import Mock, patch
from urllib.parse import urlencode

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from dojo.models import Engagement, Finding, Product, Product_Type, SLA_Configuration, Test, Test_Type

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
        self.reporter = get_user_model().objects.create_user(username="actions-reporter", email="actions@example.com")
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

    def _attach_pipeline_findings(self):
        engagement = Engagement.objects.create(
            name="Engage",
            target_start=timezone.now(),
            target_end=timezone.now(),
            product=self.product,
        )
        test_type = Test_Type.objects.create(name="Semgrep")
        dd_test = Test.objects.create(
            engagement=engagement,
            target_start=timezone.now(),
            target_end=timezone.now(),
            test_type=test_type,
        )
        Finding.objects.create(
            test=dd_test,
            title="Critical finding",
            severity="Critical",
            date=timezone.now(),
            reporter=self.reporter,
        )
        Finding.objects.create(
            test=dd_test,
            title="High finding",
            severity="High",
            date=timezone.now(),
            reporter=self.reporter,
        )
        Finding.objects.create(
            test=dd_test,
            title="Low finding",
            severity="Low",
            date=timezone.now(),
            reporter=self.reporter,
        )
        self.pipeline.tests.add(dd_test)
        self.pipeline.started = timezone.now() - timedelta(minutes=5, seconds=7)
        self.pipeline.updated = timezone.now()
        self.pipeline.save(update_fields=["started", "updated"])

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
        self.assertNotIn("AI report (CSV)", args.get("message", ""))

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

    @override_settings(SITE_URL="https://aist.itsec-europe.com")
    @patch("aist.actions.AISTSlackNotificationManager.post_message_with_token")
    def test_slack_action_default_message_includes_project_commit_and_findings_url(self, mock_post):
        branch = AISTProjectVersion.objects.create(
            project=self.project,
            version_type=VersionType.GIT_BRANCH,
            version="main",
        )
        hash_version = AISTProjectVersion.objects.create(
            project=self.project,
            version_type=VersionType.GIT_HASH,
            version="abc123def",
            resolved_from_branch=branch,
        )
        self.pipeline.project_version = hash_version
        self.pipeline.save(update_fields=["project_version", "updated"])
        action = self._make_action(
            AISTLaunchConfigAction.ActionType.PUSH_TO_SLACK,
            {"channels": ["#alerts"], "include_ai_csv": False},
            {"slack_token": "xoxb-test"},
        )

        SlackAction(action).run(pipeline=self.pipeline, new_status=AISTStatus.FINISHED)

        message = mock_post.call_args.kwargs["message"]
        self.assertIn("Project: Test Product", message)
        self.assertIn("Branch: main", message)
        self.assertIn("Commit: abc123def", message)
        self.assertIn(
            f"https://aist.itsec-europe.com{reverse('findings')}?{urlencode({'product': self.product.id, 'pipeline': self.pipeline.id})}",
            message,
        )

    @patch("aist.actions.AISTSlackNotificationManager.post_message_with_token")
    def test_slack_action_common_summary_contains_pipeline_stats(self, mock_post):
        self._attach_pipeline_findings()
        action = self._make_action(
            AISTLaunchConfigAction.ActionType.PUSH_TO_SLACK,
            {"channels": ["#alerts"], "include_common_summary": True},
            {"slack_token": "xoxb-test"},
        )

        SlackAction(action).run(pipeline=self.pipeline, new_status=AISTStatus.FINISHED)

        message = mock_post.call_args.kwargs["message"]
        self.assertIn("AIST Pipeline Summary", message)
        self.assertIn("Findings total:* 3", message)
        self.assertIn("Project version:* GIT_HASH:main", message)
        self.assertIn("Severity:* Critical: 1 | High: 1 | Medium: 0 | Low: 1 | Info: 0", message)

    @patch("aist.actions.AISTSlackNotificationManager.post_message_with_token")
    def test_slack_common_summary_uses_project_version_branch_commit_and_created_duration_fallback(self, mock_post):
        branch = AISTProjectVersion.objects.create(
            project=self.project,
            version_type=VersionType.GIT_BRANCH,
            version="release/main",
        )
        hash_version = AISTProjectVersion.objects.create(
            project=self.project,
            version_type=VersionType.GIT_HASH,
            version="abcdef1234567890",
            resolved_from_branch=branch,
        )
        self.pipeline.project_version = hash_version
        self.pipeline.save(update_fields=["project_version", "updated"])

        engagement = Engagement.objects.create(
            name="Engage Summary Branch Commit",
            target_start=timezone.now(),
            target_end=timezone.now(),
            product=self.product,
        )
        test_type = Test_Type.objects.create(name="Semgrep summary")
        dd_test = Test.objects.create(
            engagement=engagement,
            target_start=timezone.now(),
            target_end=timezone.now(),
            test_type=test_type,
            branch_tag="release/main",
            commit_hash="abcdef1234567890",
        )
        self.pipeline.tests.add(dd_test)
        self.pipeline.created = timezone.now() - timedelta(minutes=7, seconds=3)
        self.pipeline.started = self.pipeline.updated
        self.pipeline.save(update_fields=["created", "started", "updated"])

        action = self._make_action(
            AISTLaunchConfigAction.ActionType.PUSH_TO_SLACK,
            {"channels": ["#alerts"], "include_common_summary": True},
            {"slack_token": "xoxb-test"},
        )

        SlackAction(action).run(pipeline=self.pipeline, new_status=AISTStatus.FINISHED)

        message = mock_post.call_args.kwargs["message"]
        self.assertIn("Branch:* release/main", message)
        self.assertIn("Commit:* abcdef1234567890", message)
        self.assertNotIn("Duration:* 0s", message)

    @patch("aist.actions.EmailMessage.attach")
    @patch("aist.actions.EmailMessage.send")
    @patch("aist.actions.EmailNotificationManger.send_mail_notification")
    def test_email_action_with_csv_flag_sends_csv_attachment(self, mock_send_mail, mock_send_email, mock_attach):
        self._create_ai_response()
        action = self._make_action(
            AISTLaunchConfigAction.ActionType.SEND_EMAIL,
            {"emails": ["a@example.com"], "include_ai_csv": True},
        )
        EmailAction(action).run(pipeline=self.pipeline, new_status=AISTStatus.FINISHED)
        self.assertFalse(mock_send_mail.called)
        self.assertTrue(mock_send_email.called)
        self.assertTrue(mock_attach.called)
        self.assertEqual(mock_attach.call_args.kwargs["filename"], f"aist_ai_results_{self.pipeline.id}.csv")

    @patch("aist.actions.EmailNotificationManger.send_mail_notification")
    def test_email_action_sends_without_csv(self, mock_send):
        action = self._make_action(
            AISTLaunchConfigAction.ActionType.SEND_EMAIL,
            {"emails": ["a@example.com"], "include_ai_csv": False},
        )
        EmailAction(action).run(pipeline=self.pipeline, new_status=AISTStatus.FINISHED)
        self.assertTrue(mock_send.called)

    def test_email_action_fails_without_ai_response_when_csv_requested(self):
        action = self._make_action(
            AISTLaunchConfigAction.ActionType.SEND_EMAIL,
            {"emails": ["a@example.com"], "include_ai_csv": True},
        )
        with self.assertRaises(RuntimeError):
            EmailAction(action).run(pipeline=self.pipeline, new_status=AISTStatus.FINISHED)

    @override_settings(SITE_URL="https://aist.itsec-europe.com")
    @patch("aist.actions.EmailNotificationManger.send_mail_notification")
    def test_email_action_default_message_includes_project_commit_and_findings_url(self, mock_send):
        branch = AISTProjectVersion.objects.create(
            project=self.project,
            version_type=VersionType.GIT_BRANCH,
            version="main",
        )
        hash_version = AISTProjectVersion.objects.create(
            project=self.project,
            version_type=VersionType.GIT_HASH,
            version="abc123def",
            resolved_from_branch=branch,
        )
        self.pipeline.project_version = hash_version
        self.pipeline.save(update_fields=["project_version", "updated"])
        action = self._make_action(
            AISTLaunchConfigAction.ActionType.SEND_EMAIL,
            {"emails": ["a@example.com"], "include_ai_csv": False},
        )

        EmailAction(action).run(pipeline=self.pipeline, new_status=AISTStatus.FINISHED)

        kwargs = mock_send.call_args.kwargs
        self.assertIn("Test Product", kwargs["title"])
        self.assertIn("Project: Test Product", kwargs["description"])
        self.assertIn("Branch: main", kwargs["description"])
        self.assertIn("Commit: abc123def", kwargs["description"])
        self.assertIn(
            f"https://aist.itsec-europe.com{reverse('findings')}?{urlencode({'product': self.product.id, 'pipeline': self.pipeline.id})}",
            kwargs["description"],
        )

    @patch("aist.actions.EmailNotificationManger.send_mail_notification")
    def test_email_action_common_summary_contains_pipeline_stats(self, mock_send):
        self._attach_pipeline_findings()
        action = self._make_action(
            AISTLaunchConfigAction.ActionType.SEND_EMAIL,
            {"emails": ["a@example.com"], "include_common_summary": True},
        )

        EmailAction(action).run(pipeline=self.pipeline, new_status=AISTStatus.FINISHED)

        kwargs = mock_send.call_args.kwargs
        self.assertIn("AIST Pipeline Summary", kwargs["description"])
        self.assertIn("Findings total: 3", kwargs["description"])
        self.assertIn("Project version: GIT_HASH:main", kwargs["description"])
        self.assertIn("Severity: Critical: 1 | High: 1 | Medium: 0 | Low: 1 | Info: 0", kwargs["description"])

    @patch("aist.actions.install_pipeline_logging")
    def test_write_log_action_with_csv_flag_logs_simple_message(self, mock_install):
        mock_install.return_value = SimpleNamespace(info=lambda *_a, **_k: None)
        action = self._make_action(
            AISTLaunchConfigAction.ActionType.WRITE_LOG,
            {"level": "INFO", "include_ai_csv": True},
        )
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
