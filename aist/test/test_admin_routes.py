from __future__ import annotations

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse


class AdminRouteTests(TestCase):
    def _assert_no_8443_in_redirects(self, response) -> None:
        for _, location in response.redirect_chain:
            self.assertNotIn(":8443", location)
        if "Location" in response.headers:
            self.assertNotIn(":8443", response["Location"])

    def test_aist_project_ui_is_under_admin_prefix(self):
        url = reverse("aist:aist_project_list")
        self.assertTrue(url.startswith("/aist-admin/aist/"))

    def test_admin_ui_is_hidden_for_anonymous(self):
        response = self.client.get(reverse("aist:aist_project_list"))
        self.assertEqual(response.status_code, 404)

    def test_admin_ui_is_available_for_superuser(self):
        user = get_user_model().objects.create_superuser(
            username="admin_routes",
            password="pass",  # noqa: S106
            email="admin_routes@example.com",
        )
        self.client.force_login(user)
        response = self.client.get(reverse("aist:aist_project_list"))
        self.assertNotEqual(response.status_code, 404)

    def test_admin_prefix_without_trailing_slash_redirects_cleanly(self):
        response = self.client.get("/aist-admin", follow=True)
        self._assert_no_8443_in_redirects(response)
        self.assertEqual(response.status_code, 200)

    def test_login_flow_next_param_has_no_8443(self):
        response = self.client.get("/auth/login/?next=/aist-admin/aist/projects/", follow=True)
        self._assert_no_8443_in_redirects(response)
        self.assertEqual(response.status_code, 200)

    def test_plain_swagger_url_serves_aist_swagger(self):
        response = self.client.get("/api/v2/oa3/swagger-ui/")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "SwaggerUIBundle")

    def test_plain_schema_contains_only_aist_endpoints(self):
        response = self.client.get("/api/v2/oa3/schema/?format=json")
        self.assertEqual(response.status_code, 200)
        paths = response.json().get("paths", {})
        self.assertIn("/api/v2/aist/findings/", paths)
        self.assertNotIn("/api/v2/findings/", paths)
