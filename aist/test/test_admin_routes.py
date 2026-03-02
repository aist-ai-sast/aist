from __future__ import annotations

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse


class AdminRouteTests(TestCase):
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

    def test_non_superuser_cannot_open_defectdojo_form_pages(self):
        user = get_user_model().objects.create_user(
            username="client_routes",
            password="pass",  # noqa: S106
            email="client_routes@example.com",
        )
        self.client.force_login(user)
        for path in ("/aist-admin/product/add", "/aist-admin/engagement/add", "/aist-admin/test/add"):
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 403)

    def test_anonymous_cannot_open_defectdojo_form_pages(self):
        for path in ("/aist-admin/product/add", "/aist-admin/engagement/add", "/aist-admin/test/add"):
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 404)

    def test_swagger_and_schema_are_forbidden_for_anonymous_and_non_superuser(self):
        urls = (
            "/api/v2/oa3/swagger-ui/",
            "/api/v2/oa3/schema/?format=json",
            "/api/v2/oa3/swagger-ui/dojo/",
            "/api/v2/oa3/schema/dojo/?format=json",
        )
        for actor in ("anonymous", "non_superuser"):
            with self.subTest(actor=actor):
                if actor == "non_superuser":
                    user = get_user_model().objects.create_user(
                        username="client_plain_swagger",
                        password="pass",  # noqa: S106
                        email="client_plain_swagger@example.com",
                    )
                    self.client.force_login(user)
                for url in urls:
                    with self.subTest(actor=actor, url=url):
                        response = self.client.get(url)
                        self.assertEqual(response.status_code, 403)

    def test_plain_schema_contains_only_aist_endpoints_for_superuser(self):
        user = get_user_model().objects.create_superuser(
            username="admin_plain_schema",
            password="pass",  # noqa: S106
            email="admin_plain_schema@example.com",
        )
        self.client.force_login(user)
        response = self.client.get("/api/v2/oa3/schema/?format=json")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(str(body.get("openapi", "")).startswith("3."))
        paths = body.get("paths", {})
        self.assertIn("/api/v2/aist/findings/", paths)
        self.assertNotIn("/api/v2/findings/", paths)

    def test_swagger_urls_are_available_for_superuser(self):
        user = get_user_model().objects.create_superuser(
            username="admin_plain_swagger_dojo",
            password="pass",  # noqa: S106
            email="admin_plain_swagger_dojo@example.com",
        )
        self.client.force_login(user)
        cases = (
            ("/api/v2/oa3/swagger-ui/", False),
            ("/api/v2/oa3/swagger-ui/dojo/", False),
            ("/aist-admin/api/v2/oa3/swagger-ui/dojo/", False),
            ("/aist-admin/api/v2/oa3/swagger-ui/aist/dojo/", True),
        )
        for url, follow in cases:
            with self.subTest(url=url, follow=follow):
                response = self.client.get(url, follow=follow)
                self.assertEqual(response.status_code, 200)

    def test_dojo_schema_contains_only_dojo_endpoints_for_superuser(self):
        user = get_user_model().objects.create_superuser(
            username="admin_plain_schema_dojo",
            password="pass",  # noqa: S106
            email="admin_plain_schema_dojo@example.com",
        )
        self.client.force_login(user)
        response = self.client.get("/api/v2/oa3/schema/dojo/?format=json")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(str(body.get("openapi", "")).startswith("3."))
        paths = body.get("paths", {})
        self.assertIn("/aist-admin/api/v2/findings/", paths)
        self.assertNotIn("/aist-admin/api/v2/aist/findings/", paths)
