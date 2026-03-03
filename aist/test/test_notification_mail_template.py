from __future__ import annotations

from django.template.loader import render_to_string

from aist.test.test_api import AISTApiBase


class NotificationMailTemplateTests(AISTApiBase):
    def test_other_notification_template_is_branded_without_vendor_footer(self):
        rendered = render_to_string(
            "notifications/mail/other.tpl",
            {
                "description": "Pipeline finished",
                "url": None,
            },
        )

        self.assertIn("Best regards,", rendered)
        self.assertIn("AIST Security Team", rendered)
        self.assertIn("Application Security &amp; Risk Management", rendered)
        self.assertIn("/static/aist/logo.jpg", rendered)
        self.assertIn("<img", rendered)
        self.assertNotIn("Defect Dojo", rendered)
        self.assertNotIn("manage your notification settings", rendered)
        self.assertNotIn("aist-admin/notifications", rendered)

    def test_other_notification_template_preserves_multiline_summary(self):
        rendered = render_to_string(
            "notifications/mail/other.tpl",
            {
                "description": "Line 1\nLine 2\nLine 3",
                "url": None,
            },
        )

        self.assertIn("Line 1<br", rendered)
        self.assertIn("Line 2<br", rendered)
        self.assertIn("Line 3", rendered)

    def test_other_notification_template_renders_markdown_bold(self):
        rendered = render_to_string(
            "notifications/mail/other.tpl",
            {
                "description": "**Status:** FINISHED",
                "url": None,
            },
        )

        self.assertIn("<strong>Status:</strong>", rendered)
        self.assertIn("FINISHED", rendered)
