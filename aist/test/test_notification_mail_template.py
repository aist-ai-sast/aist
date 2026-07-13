from __future__ import annotations

from types import SimpleNamespace

from django.template import Context, Template
from django.template.loader import render_to_string

from aist.test.test_api import AISTApiBase


def _finding(pk, title, severity="High"):
    return SimpleNamespace(id=pk, title=title, severity=severity, status="Active", sla_age=3)


class _Named:

    """Lightweight fake with an .id and a str() label — cheaper than a real model row."""

    def __init__(self, pk, label, **extra):
        self.id = pk
        self._label = label
        self.__dict__.update(extra)

    def __str__(self):
        return self._label


class SharedNotificationBaseTests(AISTApiBase):

    """
    _base.tpl absorbs the boilerplate every DefectDojo notification template
    repeated (intro line, notification-settings link, disclaimer box) plus
    the "Defect Dojo" fallback signature none of them should show anymore.
    other.tpl overrides the footer to skip the settings link (it's AIST's own
    action-alert channel, not a user notification preference) — the DEFAULT
    behavior for the other 12 templates is exercised here directly.
    """

    def test_default_footer_includes_notification_settings_link(self):
        rendered = Template(
            '{% extends "notifications/mail/_base.tpl" %}{% block body %}hi{% endblock %}',
        ).render(Context({}))
        self.assertIn("manage your notification settings", rendered)
        self.assertNotIn("Defect Dojo", rendered)
        self.assertIn("<img", rendered)
        self.assertIn("/static/aist/logo.jpg", rendered)

    def test_disclaimer_shown_when_configured(self):
        rendered = Template(
            '{% extends "notifications/mail/_base.tpl" %}{% block body %}hi{% endblock %}',
        ).render(Context({"system_settings": {"disclaimer_notifications": "Confidential test message"}}))
        self.assertIn("Confidential test message", rendered)


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


class RebasedVendorTemplatesTests(AISTApiBase):

    """
    Each of these 12 templates overrides vendor/defectdojo's own version of
    the same name (never modified directly — vendor/ is read-only). Django's
    template loader resolves aist/templates/notifications/mail/<event>.tpl
    ahead of the vendor copy automatically, the same mechanism other.tpl's
    override already relied on. Every one of these previously printed
    "Defect Dojo" as a fallback signature when system_settings.team_name was
    blank (the default in every fixture) — that fallback is gone now that the
    shared _base.tpl supplies the signature once, globally.
    """

    def test_product_added(self):
        rendered = render_to_string(
            "notifications/mail/product_added.tpl",
            {"title": "New Product", "url": "/product/1/"},
        )
        self.assertIn("New Product", rendered)
        self.assertNotIn("Defect Dojo", rendered)
        self.assertIn("<img", rendered)

    def test_product_type_added(self):
        rendered = render_to_string(
            "notifications/mail/product_type_added.tpl",
            {"title": "New Org", "url": "/product_type/1/"},
        )
        self.assertIn("New Org", rendered)
        self.assertNotIn("Defect Dojo", rendered)

    def test_engagement_added(self):
        product = _Named(1, "Demo Product")
        engagement = _Named(10, "Q1 Pentest", product=product, name="Q1 Pentest")
        rendered = render_to_string(
            "notifications/mail/engagement_added.tpl",
            {"engagement": engagement},
        )
        self.assertIn("Q1 Pentest", rendered)
        self.assertIn("Demo Product", rendered)
        self.assertNotIn("Defect Dojo", rendered)

    def test_engagement_closed(self):
        product = _Named(1, "Demo Product")
        engagement = _Named(10, "Q1 Pentest", product=product, name="Q1 Pentest")
        rendered = render_to_string(
            "notifications/mail/engagement_closed.tpl",
            {"engagement": engagement},
        )
        self.assertIn("Q1 Pentest", rendered)
        self.assertIn("has been closed", rendered)
        self.assertNotIn("Defect Dojo", rendered)

    def test_test_added(self):
        product = _Named(1, "Demo Product")
        engagement = _Named(10, "Q1 Pentest", product=product)
        test_obj = _Named(20, "Semgrep scan", engagement=engagement)
        rendered = render_to_string(
            "notifications/mail/test_added.tpl",
            {"test": test_obj, "engagement": engagement, "product": product, "user": self.user},
        )
        self.assertIn("A new test has been added", rendered)
        self.assertNotIn("Defect Dojo", rendered)

    def test_upcoming_engagement(self):
        engagement = _Named(10, "Q1 Pentest", product=_Named(1, "Demo Product"), target_start="2026-01-01", target_end="2026-01-15")
        rendered = render_to_string(
            "notifications/mail/upcoming_engagement.tpl",
            {"engagement": engagement},
        )
        self.assertIn("about to start shortly", rendered)
        self.assertIn("2026-01-01", rendered)
        self.assertNotIn("Defect Dojo", rendered)

    def test_review_requested(self):
        rendered = render_to_string(
            "notifications/mail/review_requested.tpl",
            {
                "requested_by": "alice",
                "finding": "SQL Injection",
                "reviewers": [self.user],
                "note": "Please double-check this one",
                "url": "/finding/1/",
            },
        )
        self.assertIn("SQL Injection", rendered)
        self.assertIn("Please double-check this one", rendered)
        self.assertNotIn("Defect Dojo", rendered)

    def test_user_mentioned(self):
        rendered = render_to_string(
            "notifications/mail/user_mentioned.tpl",
            {
                "requested_by": "alice",
                "section": "Finding #42",
                "note": "@bob take a look",
                "url": "/finding/42/",
            },
        )
        self.assertIn("Finding #42", rendered)
        self.assertIn("@bob take a look", rendered)
        self.assertNotIn("Defect Dojo", rendered)

    def test_risk_acceptance_expiration(self):
        engagement = _Named(10, "Q1 Pentest", product=_Named(1, "Demo Product"))
        risk_acceptance = _Named(
            30,
            "RA-1",
            engagement=engagement,
            is_expired=True,
            reactivate_expired=True,
            restart_sla_expired=False,
            expiration_date_handled="2026-01-01",
            accepted_findings=SimpleNamespace(all=lambda: [_finding(1, "Weak crypto")]),
        )
        rendered = render_to_string(
            "notifications/mail/risk_acceptance_expiration.tpl",
            {"description": "Risk acceptance has expired", "risk_acceptance": risk_acceptance},
        )
        self.assertIn("Weak crypto", rendered)
        self.assertIn("has expired", rendered)
        self.assertIn("Findings have been reactivated", rendered)
        self.assertNotIn("Defect Dojo", rendered)

    def test_scan_added(self):
        product = _Named(1, "Demo Product")
        engagement = _Named(10, "Q1 Pentest", product=product)
        test_obj = _Named(20, "Semgrep scan", engagement=engagement)
        rendered = render_to_string(
            "notifications/mail/scan_added.tpl",
            {
                "description": "Scan uploaded",
                "test": test_obj,
                "engagement": engagement,
                "product": product,
                "finding_count": 4,
                "findings_new": [_finding(1, "New XSS")],
                "findings_reactivated": [],
                "findings_mitigated": [],
                "findings_untouched": [],
            },
        )
        self.assertIn("New XSS", rendered)
        self.assertIn("New findings", rendered)
        self.assertNotIn("Defect Dojo", rendered)

    def test_sla_breach(self):
        rendered = render_to_string(
            "notifications/mail/sla_breach.tpl",
            {"finding": _finding(1, "Hardcoded secret"), "sla": 3, "sla_age": -3, "user": self.user},
        )
        self.assertIn("Hardcoded secret", rendered)
        self.assertIn("breached its SLA", rendered)
        self.assertNotIn("Defect Dojo", rendered)

    def test_sla_breach_combined(self):
        product = _Named(1, "Demo Product", team_manager="Alice", product_manager="Bob", technical_contact="Carol")
        rendered = render_to_string(
            "notifications/mail/sla_breach_combined.tpl",
            {
                "product": product,
                "breach_kind": "breached",
                "findings": [_finding(1, "Path traversal")],
                "user": self.user,
            },
        )
        self.assertIn("Path traversal", rendered)
        self.assertIn("have breached their SLA", rendered)
        self.assertNotIn("Defect Dojo", rendered)
