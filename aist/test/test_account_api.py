from __future__ import annotations

from unittest.mock import patch

from django.contrib.auth.models import Group
from django.test import Client
from django.urls import reverse
from dojo.authorization.roles_permissions import Roles
from dojo.models import Dojo_Group, Dojo_Group_Member, Product_Type_Group, Role
from rest_framework.test import APIClient

from aist.models import Organization
from aist.test.test_api import AISTApiBase


class AISTAccountAPITests(AISTApiBase):
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

    def test_auth_login_csrf_behavior(self):
        client = Client(enforce_csrf_checks=True)
        login_url = reverse("aist_api:auth_login")

        without_csrf = client.post(
            login_url,
            data={"username": self.user.username, "password": "pass"},
            content_type="application/json",
        )
        self.assertEqual(without_csrf.status_code, 403)

        client.get(reverse("client_login"))
        csrf_token = client.cookies["csrftoken"].value
        with_csrf = client.post(
            login_url,
            data={"username": self.user.username, "password": "pass"},
            content_type="application/json",
            HTTP_X_CSRFTOKEN=csrf_token,
        )
        self.assertEqual(with_csrf.status_code, 204)

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

    def test_me_get_returns_group_based_membership(self):
        user = self.user.__class__.objects.create_user(
            username="group-member",
            email="group-member@example.com",
            password="pass",  # noqa: S106
        )
        client = APIClient()
        client.force_authenticate(user=user)

        organization = Organization.objects.create(name="Group Org")
        organization.product_type = self.prod_type
        organization.save(update_fields=["product_type"])
        self.project.organization = organization
        self.project.save(update_fields=["organization"])

        auth_group = Group.objects.create(name="aist-group")
        group = Dojo_Group.objects.create(name="aist-group", auth_group=auth_group)
        user.groups.add(auth_group)
        role_reader, _ = Role.objects.get_or_create(id=Roles.Reader, defaults={"name": "Reader"})
        Dojo_Group_Member.objects.create(group=group, user=user, role=role_reader)
        Product_Type_Group.objects.create(product_type=self.prod_type, group=group, role=role_reader)

        with patch("aist.api.account.get_system_setting", return_value=True):
            response = client.get(reverse("aist_api:me"))

        self.assertEqual(response.status_code, 200)
        memberships = response.data.get("organization_memberships", [])
        self.assertEqual(len(memberships), 1)
        self.assertEqual(memberships[0]["organization_name"], "Group Org")
        self.assertEqual(memberships[0]["role_name"], "Reader")

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

    def test_login_rotates_csrf_token(self):
        # Django's login() calls rotate_token(), which changes the CSRF cookie.
        # The SPA must be able to read the new value (requires CSRF_COOKIE_HTTPONLY=False).
        client = Client(enforce_csrf_checks=True)
        client.get(reverse("client_login"))
        csrf_before = client.cookies["csrftoken"].value
        client.post(
            reverse("aist_api:auth_login"),
            data={"username": self.user.username, "password": "pass"},
            content_type="application/json",
            HTTP_X_CSRFTOKEN=csrf_before,
        )
        csrf_after = client.cookies["csrftoken"].value
        self.assertNotEqual(csrf_before, csrf_after, "CSRF token must be rotated after login()")

    def test_logout_invalidates_session(self):
        # Full sign-out scenario: after logout the session must be gone.
        # Uses the post-login CSRF token (as a browser with CSRF_COOKIE_HTTPONLY=False would).
        client = Client(enforce_csrf_checks=True)
        client.get(reverse("client_login"))
        csrf_before = client.cookies["csrftoken"].value
        client.post(
            reverse("aist_api:auth_login"),
            data={"username": self.user.username, "password": "pass"},
            content_type="application/json",
            HTTP_X_CSRFTOKEN=csrf_before,
        )
        # Use the rotated post-login CSRF token for the logout call.
        csrf_after_login = client.cookies["csrftoken"].value
        response = client.post(
            reverse("aist_api:auth_logout"),
            content_type="application/json",
            HTTP_X_CSRFTOKEN=csrf_after_login,
        )
        self.assertEqual(response.status_code, 204)
        # Session must be gone — authenticated endpoint returns 401/403.
        response = client.get(reverse("aist_api:me"))
        self.assertIn(response.status_code, [401, 403])

    def test_relogin_after_logout(self):
        # A second login after sign-out must succeed (no stale CSRF blocks it).
        client = Client(enforce_csrf_checks=True)
        client.get(reverse("client_login"))
        csrf_before = client.cookies["csrftoken"].value
        client.post(
            reverse("aist_api:auth_login"),
            data={"username": self.user.username, "password": "pass"},
            content_type="application/json",
            HTTP_X_CSRFTOKEN=csrf_before,
        )
        csrf_after_login = client.cookies["csrftoken"].value
        client.post(
            reverse("aist_api:auth_logout"),
            content_type="application/json",
            HTTP_X_CSRFTOKEN=csrf_after_login,
        )
        # After logout, re-login should work with a fresh CSRF token.
        client.get(reverse("client_login"))
        csrf_fresh = client.cookies["csrftoken"].value
        response = client.post(
            reverse("aist_api:auth_login"),
            data={"username": self.user.username, "password": "pass"},
            content_type="application/json",
            HTTP_X_CSRFTOKEN=csrf_fresh,
        )
        self.assertEqual(response.status_code, 204)
