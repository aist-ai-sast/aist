"""
Task 11 — superuser can manage OrgIntegration rows via Django admin.

This is the fallback management surface for org integrations (the
primary one is client-ui / REST API). Used by AIST operators when
something is misconfigured at REST level (DNS/auth) and they need to
edit directly via the database admin UI.
"""
from __future__ import annotations

from django.contrib.admin.sites import site as admin_site
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from dojo.models import Product_Type

from aist.models import Organization, OrgIntegration, OrgIntegrationType


class OrgIntegrationAdminTests(TestCase):

    def setUp(self):
        self.superuser = get_user_model().objects.create_superuser(
            username="admin", password="x", email="admin@example.com",  # noqa: S106
        )
        self.client.force_login(self.superuser)
        self.org = Organization.objects.create(
            name="Admin Org",
            product_type=Product_Type.objects.create(name="Admin PT"),
        )

    def test_org_integration_is_registered_in_admin(self):
        self.assertIn(OrgIntegration, admin_site._registry)

    def test_admin_changelist_renders_for_claude_integration(self):
        OrgIntegration.objects.create(
            organization=self.org,
            integration_type=OrgIntegrationType.CLAUDE_CODE,
            name="primary",
            secret="sk-ant-oat01-admin-test-token-12345",  # noqa: S106
            is_active=True,
            config={"auth_mode": "oauth"},
        )
        url = reverse("admin:aist_orgintegration_changelist")
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)
        # The integration must be listed; secret value must NOT appear
        # in the rendered HTML (admin never displays encrypted plaintext).
        self.assertContains(resp, "primary")
        self.assertNotContains(resp, "sk-ant-oat01-admin-test-token-12345")

    def test_admin_change_form_does_not_leak_secret_value(self):
        integration = OrgIntegration.objects.create(
            organization=self.org,
            integration_type=OrgIntegrationType.CLAUDE_CODE,
            name="primary",
            secret="sk-ant-oat01-admin-test-token-67890",  # noqa: S106
            is_active=True,
            config={"auth_mode": "oauth"},
        )
        url = reverse("admin:aist_orgintegration_change", args=[integration.pk])
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)
        # Even the change form should not echo the secret value back —
        # the admin must render it masked or excluded entirely so that
        # screen-shares / browser history don't leak it.
        self.assertNotContains(resp, "sk-ant-oat01-admin-test-token-67890")
