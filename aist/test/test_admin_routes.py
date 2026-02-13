from __future__ import annotations

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse


class AdminRouteTests(TestCase):
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
