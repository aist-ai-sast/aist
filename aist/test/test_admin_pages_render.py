"""
The admin pages a superuser opens actually render.

The route tests next door answer who may reach these pages; this one answers whether the
page comes back at all. They deliberately overlap: a template that stops rendering after a
platform upgrade returns 500, which "not a 404" would have accepted.
"""
from __future__ import annotations

import re

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

# Pages that render from an empty database. Anything needing a specific object belongs in
# the test that creates it.
ADMIN_PAGES = (
    "aist:aist_project_list",
    "aist:pipeline_list",
    "aist:launching_dashboard",
    "aist:ai_filter_help",
    "aist:ai_filter_reference",
    "aist:start_pipeline",
)

# A <dialog> is closed until script opens it. One written open in the markup covers the
# page, which is how the Bootstrap-era modals behaved once their plugin went away.
OPEN_DIALOG = re.compile(r"<dialog[^>]*\bopen\b[^>]*>")


class AdminPageRenderTests(TestCase):
    def setUp(self):
        self.superuser = get_user_model().objects.create_superuser(
            username="render_admin",
            password="pass",  # noqa: S106
            email="render_admin@example.com",
        )
        self.client.force_login(self.superuser)

    def test_every_aist_admin_page_renders(self):
        for name in ADMIN_PAGES:
            with self.subTest(page=name):
                response = self.client.get(reverse(name))
                self.assertEqual(response.status_code, 200, response.content[:400])

    def test_the_platform_dashboard_renders(self):
        """It extends our base template, so a broken base shows up here first."""
        response = self.client.get(reverse("dashboard"))
        self.assertEqual(response.status_code, 200, response.content[:400])

    def test_no_page_renders_a_dialog_already_open(self):
        """A modal must wait for its trigger; an open one covers the page it belongs to."""
        for name in ADMIN_PAGES:
            with self.subTest(page=name):
                body = self.client.get(reverse(name)).content.decode()
                self.assertIsNone(OPEN_DIALOG.search(body), f"{name} renders an open dialog")

    def test_a_rendered_page_uses_our_base_template(self):
        """Without this the pages could render from the platform's own base and still pass."""
        response = self.client.get(reverse("aist:pipeline_list"))
        self.assertIn("AIST Pipelines", response.content.decode())
