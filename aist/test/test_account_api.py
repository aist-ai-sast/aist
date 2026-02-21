from __future__ import annotations

from unittest.mock import patch

from django.test import Client
from django.urls import reverse
from rest_framework.test import APIClient

from aist.models import Organization
from aist.test.test_api import AISTApiBase


class AISTAccountAPITests(AISTApiBase):
    def test_auth_login_returns_204_for_valid_credentials(self):
        client = APIClient()
        response = client.post(
            reverse("aist_api:auth_login"),
            data={"username": self.user.username, "password": "pass"},
            format="json",
        )
        self.assertEqual(response.status_code, 204)

    def test_auth_login_rejects_invalid_credentials(self):
        client = Client(enforce_csrf_checks=True)
        client.get(reverse("client_login"))
        csrf_token = client.cookies["csrftoken"].value
        response = client.post(
            reverse("aist_api:auth_login"),
            data={"username": self.user.username, "password": "wrong"},
            content_type="application/json",
            HTTP_X_CSRFTOKEN=csrf_token,
        )
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json().get("detail"), "Invalid username or password.")

    def test_auth_login_rejects_without_csrf_token(self):
        client = Client(enforce_csrf_checks=True)
        response = client.post(
            reverse("aist_api:auth_login"),
            data={"username": self.user.username, "password": "pass"},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 403)

    def test_auth_login_accepts_with_csrf_token(self):
        client = Client(enforce_csrf_checks=True)
        client.get(reverse("client_login"))
        csrf_token = client.cookies["csrftoken"].value
        response = client.post(
            reverse("aist_api:auth_login"),
            data={"username": self.user.username, "password": "pass"},
            content_type="application/json",
            HTTP_X_CSRFTOKEN=csrf_token,
        )
        self.assertEqual(response.status_code, 204)

    def test_me_get_returns_profile(self):
        organization = Organization.objects.create(name="Access Org")
        organization.product_type = self.prod_type
        organization.save(update_fields=["product_type"])
        self.project.organization = organization
        self.project.save(update_fields=["organization"])

        with patch("aist.api.account.get_system_setting", return_value=True):
            response = self.client.get(reverse("aist_api:me"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["username"], self.user.username)
        self.assertIn("can_edit_profile", response.data)
        self.assertIn("can_edit_username", response.data)
        memberships = response.data.get("organization_memberships", [])
        self.assertEqual(len(memberships), 1)
        self.assertEqual(memberships[0]["organization_name"], "Access Org")
        self.assertEqual(memberships[0]["role_name"], "Maintainer")

    def test_me_patch_updates_profile(self):
        with patch("aist.api.account.get_system_setting", return_value=True):
            response = self.client.patch(
                reverse("aist_api:me"),
                data={
                    "first_name": "Test",
                    "last_name": "User",
                    "email": "test-user@example.com",
                    "username": "tester-updated",
                },
                format="json",
            )
        self.assertEqual(response.status_code, 200)
        self.user.refresh_from_db()
        self.assertEqual(self.user.first_name, "Test")
        self.assertEqual(self.user.last_name, "User")
        self.assertEqual(self.user.email, "test-user@example.com")
        self.assertEqual(self.user.username, "tester-updated")

    def test_me_patch_rejected_when_profile_edit_disabled(self):
        with patch("aist.api.account.get_system_setting", return_value=False):
            response = self.client.patch(
                reverse("aist_api:me"),
                data={"first_name": "Blocked"},
                format="json",
            )
        self.assertEqual(response.status_code, 400)

    def test_change_password_success(self):
        response = self.client.post(
            reverse("aist_api:me_change_password"),
            data={
                "current_password": "pass",
                "new_password": "N3wPass!123",
                "new_password_confirm": "N3wPass!123",
            },
            format="json",
        )
        self.assertEqual(response.status_code, 200)
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password("N3wPass!123"))

    def test_change_password_rejects_wrong_current_password(self):
        response = self.client.post(
            reverse("aist_api:me_change_password"),
            data={
                "current_password": "wrong",
                "new_password": "N3wPass!123",
                "new_password_confirm": "N3wPass!123",
            },
            format="json",
        )
        self.assertEqual(response.status_code, 400)

    def test_auth_logout_returns_204(self):
        response = self.client.post(reverse("aist_api:auth_logout"), format="json")
        self.assertEqual(response.status_code, 204)

    @patch("aist.api.account.remove_all_sessions")
    def test_auth_logout_all_uses_single_session_mechanism(self, remove_all_sessions):
        response = self.client.post(reverse("aist_api:auth_logout_all"), format="json")
        self.assertEqual(response.status_code, 204)
        remove_all_sessions.assert_called_once()
