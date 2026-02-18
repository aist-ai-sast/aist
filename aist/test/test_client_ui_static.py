from __future__ import annotations

import json
import re

from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase


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

    def test_runtime_routes_include_expected_api_endpoints(self):
        response = self.client.get("/pipelines")
        self.assertEqual(response.status_code, 200)

        html = response.content.decode("utf-8")
        routes = self._extract_routes_json(html)

        self.assertEqual(routes["login_url"], "/auth/login/")
        self.assertEqual(routes["logout_url"], "/auth/logout/")
        self.assertIn("{id}", routes["finding_detail_url"])
        self.assertIn("{id}", routes["finding_close_url"])
        self.assertIn("{pipeline_id}", routes["pipeline_export_url"])
        self.assertEqual(routes["ai_finding_responses_url"], "/api/v2/aist/ai-finding-responses/")
        self.assertIn("{project_version_id}", routes["project_version_file_url"])
        self.assertIn("{subpath}", routes["project_version_file_url"])
        self.assertEqual(routes["ui_findings_path"], "/findings")
        self.assertIn(":id", routes["ui_finding_detail_path"])


class ClientPortalAuthFlowTests(TestCase):
    def test_login_route_is_available(self):
        response = self.client.get("/auth/login/")
        self.assertIn(response.status_code, (200, 302))

    def test_logout_clears_authenticated_session(self):
        user = get_user_model().objects.create_user(
            username="client_portal_auth_user",
            email="client-portal-auth@example.com",
        )
        self.client.force_login(user)

        response = self.client.get("/auth/logout/")
        self.assertIn(response.status_code, (200, 302))
        self.assertNotIn("_auth_user_id", self.client.session)
