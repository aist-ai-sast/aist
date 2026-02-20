from __future__ import annotations

import json
import re

from django.contrib.auth import get_user_model
from django.test import Client, SimpleTestCase, TestCase


class ClientPortalRouteTests(SimpleTestCase):
    def _extract_routes_json(self, html: str) -> dict:
        match = re.search(r"window\.__AIST_ROUTES__\s*=\s*(\{.*?\});", html, flags=re.DOTALL)
        self.assertIsNotNone(match)
        return json.loads(match.group(1))

    def test_root_redirects_to_findings(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], "/findings")

    def test_client_side_route_fallback_renders_same_shell(self):
        response = self.client.get("/findings/123")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, '<div id="root"></div>', html=True)
        self.assertContains(response, "window.__AIST_ROUTES__")

    def test_anonymous_can_open_all_client_ui_routes(self):
        for path in ("/findings", "/products", "/pipelines", "/search", "/settings", "/finding/1"):
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 200)
                self.assertContains(response, '<div id="root"></div>', html=True)
                self.assertContains(response, "window.__AIST_ROUTES__")

    def test_runtime_routes_include_expected_api_endpoints(self):
        response = self.client.get("/pipelines")
        self.assertEqual(response.status_code, 200)

        html = response.content.decode("utf-8")
        routes = self._extract_routes_json(html)

        self.assertEqual(routes["login_url"], "/auth/login/")
        self.assertEqual(routes["login_api_url"], "/api/v2/aist/auth/login/")
        self.assertEqual(routes["logout_url"], "/api/v2/aist/auth/logout/")
        self.assertIn("logout_all_devices_url", routes)
        self.assertIn("me_url", routes)
        self.assertIn("me_change_password_url", routes)
        self.assertIn("{id}", routes["finding_detail_url"])
        self.assertIn("{id}", routes["finding_close_url"])
        self.assertIn("{finding_id}", routes["finding_notes_url"])
        self.assertIn("{finding_id}", routes["finding_export_url"])
        self.assertIn("{pipeline_id}", routes["pipeline_export_url"])
        self.assertEqual(routes["ai_finding_responses_url"], "/api/v2/aist/ai-finding-responses/")
        self.assertIn("{project_version_id}", routes["project_version_file_url"])
        self.assertIn("{subpath}", routes["project_version_file_url"])
        self.assertEqual(routes["ui_findings_path"], "/findings")
        self.assertIn(":id", routes["ui_finding_detail_path"])


class ClientPortalAuthFlowTests(TestCase):
    def test_login_route_is_available(self):
        response = self.client.get("/auth/login/")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, '<div id="root"></div>', html=True)

    def test_anonymous_is_rejected_from_authenticated_aist_api(self):
        for path in ("/api/v2/aist/me/", "/api/v2/aist/projects/", "/api/v2/aist/findings/"):
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertIn(response.status_code, (401, 403))

    def test_logout_clears_authenticated_session(self):
        user = get_user_model().objects.create_user(
            username="client_portal_auth_user",
            email="client-portal-auth@example.com",
        )
        self.client.force_login(user)

        response = self.client.get("/auth/logout/")
        self.assertIn(response.status_code, (200, 302))
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_logout_does_not_invalidate_other_client_session(self):
        user = get_user_model().objects.create_user(
            username="client_portal_multi_session_user",
            email="client-portal-multi-session@example.com",
        )

        first_client = Client()
        second_client = Client()
        first_client.force_login(user)
        second_client.force_login(user)

        logout_response = second_client.get("/auth/logout/")
        self.assertIn(logout_response.status_code, (200, 302))

        first_profile = first_client.get("/aist-admin/api/v2/user_profile/")
        self.assertEqual(first_profile.status_code, 200)

    def test_logout_all_devices_invalidates_current_session(self):
        user = get_user_model().objects.create_user(
            username="client_portal_logout_all_current",
            email="client-portal-logout-all-current@example.com",
        )
        client = Client()
        client.force_login(user)

        response = client.post("/auth/logout-all/")
        self.assertIn(response.status_code, (200, 302))

        profile = client.get("/aist-admin/api/v2/user_profile/")
        self.assertIn(profile.status_code, (401, 403))

    def test_logout_all_devices_invalidates_other_user_sessions(self):
        user = get_user_model().objects.create_user(
            username="client_portal_logout_all_enabled",
            email="client-portal-logout-all-enabled@example.com",
        )
        first_client = Client()
        second_client = Client()
        first_client.force_login(user)
        second_client.force_login(user)

        response = second_client.post("/auth/logout-all/")
        self.assertIn(response.status_code, (200, 302))

        first_profile = first_client.get("/aist-admin/api/v2/user_profile/")
        second_profile = second_client.get("/aist-admin/api/v2/user_profile/")
        self.assertIn(first_profile.status_code, (401, 403))
        self.assertIn(second_profile.status_code, (401, 403))
