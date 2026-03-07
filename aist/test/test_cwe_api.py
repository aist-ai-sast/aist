from __future__ import annotations

from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from rest_framework.test import APIClient


class CweDetailApiTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = get_user_model().objects.create_user(
            username="cwe_test_user",
            email="cwe@example.com",
            password="pass",  # noqa: S106
        )
        self.client.force_authenticate(user=self.user)

    def _url(self, cwe_id: int) -> str:
        return reverse("aist_api:cwe_detail", kwargs={"cwe_id": cwe_id})

    def test_returns_cwe_metadata_for_known_cwe(self):
        fake_meta = {
            "title": "Improper Input Validation",
            "description": "The product does not validate input properly.",
            "impact": "Varies",
            "url": "https://cwe.mitre.org/data/definitions/20.html",
        }
        with patch("aist.api.cwe.fetch_cwe_meta", return_value=fake_meta):
            response = self.client.get(self._url(20))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["title"], "Improper Input Validation")
        self.assertIn("description", response.data)
        self.assertIn("url", response.data)

    def test_returns_404_for_unknown_cwe(self):
        with patch("aist.api.cwe.fetch_cwe_meta", return_value=None):
            response = self.client.get(self._url(999999))
        self.assertEqual(response.status_code, 404)

    def test_returns_400_for_invalid_cwe_id(self):
        response = self.client.get(self._url(0))
        self.assertEqual(response.status_code, 400)

    def test_requires_authentication(self):
        unauthenticated = APIClient()
        with patch("aist.api.cwe.fetch_cwe_meta", return_value={"title": "X", "description": "", "impact": "", "url": ""}):
            response = unauthenticated.get(self._url(20))
        self.assertEqual(response.status_code, 403)

    def test_uses_cache_on_second_request(self):
        fake_meta = {"title": "Cached CWE", "description": "desc", "impact": "imp", "url": "http://example.com"}
        with patch("aist.api.cwe.fetch_cwe_meta", return_value=fake_meta) as mock_fetch:
            self.client.get(self._url(79))
            self.client.get(self._url(79))
        # fetch_cwe_meta should only be called once; the second hit comes from cache
        self.assertEqual(mock_fetch.call_count, 1)
