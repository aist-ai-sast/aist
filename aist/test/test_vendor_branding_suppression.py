"""
No DefectDojo upsell reaches an AIST API caller or page.

The vendor attaches a ``pro`` key to the error bodies it renders; AIST is not presented
to its users as DefectDojo, so that name must not leave this API or its templates.
"""
from __future__ import annotations

from pathlib import Path

from django.conf import settings
from django.test import SimpleTestCase
from rest_framework.exceptions import NotFound, PermissionDenied
from rest_framework.views import APIView

from aist.api.exception_handler import aist_exception_handler, strip_vendor_upsell

_TEMPLATES = Path(__file__).resolve().parent.parent / "templates"


class ExceptionHandlerTests(SimpleTestCase):
    def _handle(self, exc):
        return aist_exception_handler(exc, {"view": APIView(), "request": None})

    def test_a_denial_carries_no_vendor_upsell(self):
        response = self._handle(PermissionDenied("nope"))
        self.assertIsNotNone(response)
        self.assertEqual(response.status_code, 403)
        self.assertNotIn("pro", response.data)

    def test_a_not_found_carries_no_vendor_upsell(self):
        response = self._handle(NotFound("gone"))
        self.assertNotIn("pro", response.data)

    def test_the_rest_of_the_body_survives(self):
        """Stripping must remove the advertisement, not the error the caller needs."""
        response = self._handle(PermissionDenied("nope"))
        self.assertTrue(response.data, "the denial detail must still be reported")

    def test_the_handler_is_the_configured_one(self):
        self.assertEqual(
            settings.REST_FRAMEWORK["EXCEPTION_HANDLER"],
            "aist.api.exception_handler.aist_exception_handler",
        )

    def test_a_list_body_is_cleaned_too(self):
        body = [{"pro": ["ad"], "detail": "x"}, {"detail": "y"}]
        self.assertEqual(strip_vendor_upsell(body), [{"detail": "x"}, {"detail": "y"}])

    def test_a_body_that_never_had_one_is_unchanged(self):
        self.assertEqual(strip_vendor_upsell({"detail": "x"}), {"detail": "x"})


class TemplateBrandingTests(SimpleTestCase):
    def test_our_templates_name_no_vendor_domain(self):
        offenders = [
            str(path.relative_to(_TEMPLATES))
            for path in _TEMPLATES.rglob("*.html")
            if "defectdojo.com" in path.read_text(encoding="utf-8").lower()
        ]
        self.assertEqual(offenders, [], f"vendor domain reachable from the UI in: {offenders}")
