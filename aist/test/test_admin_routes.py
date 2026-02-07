from __future__ import annotations

from django.test import SimpleTestCase
from django.urls import reverse


class AdminRouteTests(SimpleTestCase):
    def test_aist_project_ui_is_under_admin_prefix(self):
        url = reverse("aist:aist_project_list")
        self.assertTrue(url.startswith("/aist-admin/aist/"))
