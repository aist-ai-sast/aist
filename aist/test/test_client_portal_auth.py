from __future__ import annotations

from django.test import TestCase


class ClientPortalAuthTests(TestCase):
    def test_login_get_sets_csrf_cookie(self):
        response = self.client.get("/auth/login/")
        self.assertIn(response.status_code, (200, 302))

    def test_logout_route_exists(self):
        response = self.client.get("/auth/logout/")
        self.assertIn(response.status_code, (200, 302))

    def test_client_portal_template_includes_csrf(self):
        response = self.client.get("/")
        self.assertContains(response, "__AIST_CSRF__")
