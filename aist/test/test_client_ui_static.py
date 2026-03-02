from __future__ import annotations

from django.contrib.auth import get_user_model
from django.test import Client, SimpleTestCase, TestCase, override_settings

DJANGO_VITE_TEST_SETTINGS = {
    "default": {
        "dev_mode": True,
        "manifest_path": "/non-existent-manifest-for-tests.json",
        "dev_server_protocol": "http",
        "dev_server_host": "localhost",
        "dev_server_port": 5173,
        "static_url_prefix": "/",
    },
}


@override_settings(DJANGO_VITE=DJANGO_VITE_TEST_SETTINGS)
class ClientPortalRouteTests(SimpleTestCase):
    def test_html_shell_has_no_store_cache_headers(self):
        response = self.client.get("/dashboard")
        self.assertEqual(response.status_code, 200)
        self.assertIn("no-store", response.get("Cache-Control", ""))
        self.assertEqual(response.get("Pragma"), "no-cache")
        self.assertEqual(response.get("Expires"), "0")


@override_settings(DJANGO_VITE=DJANGO_VITE_TEST_SETTINGS)
class ClientPortalAuthFlowTests(TestCase):
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
