from __future__ import annotations

from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.cache import cache
from django.db import IntegrityError, transaction
from django.test import Client
from django.urls import reverse
from dojo.authorization.roles_permissions import Roles
from dojo.models import (
    Dojo_Group,
    Dojo_Group_Member,
    Product,
    Product_Type,
    Product_Type_Group,
    Product_Type_Member,
    Role,
)
from rest_framework.test import APIClient

from aist.models import AISTProject, Organization
from aist.test.test_api import AISTApiBase


class AISTAccountAPITests(AISTApiBase):
    def setUp(self):
        super().setUp()
        # This class alone makes a dozen+ real POSTs to auth_login, which is
        # rate-limited by ScopedRateThrottle (aist_auth_login, 10/min default).
        # The throttle cache persists across test methods within the same
        # run, so without clearing it here, later tests intermittently see a
        # spurious 429 instead of the status they're actually asserting on.
        cache.clear()

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

    def test_login_by_email_succeeds(self):
        # Invited accounts store username = email local-part and email = the
        # full address (aist/members/service.py's _unique_username), and every
        # UI surface displays the email — so login must accept it too, not
        # just the (never-shown) username. Regression test for
        # aist.auth_backends.EmailBackend.
        client = Client(enforce_csrf_checks=True)
        client.get(reverse("client_login"))
        csrf_token = client.cookies["csrftoken"].value
        response = client.post(
            reverse("aist_api:auth_login"),
            data={"username": self.user.email, "password": "pass"},
            content_type="application/json",
            HTTP_X_CSRFTOKEN=csrf_token,
        )
        self.assertEqual(response.status_code, 204)

    def test_login_by_email_wrong_password_rejected(self):
        client = Client(enforce_csrf_checks=True)
        client.get(reverse("client_login"))
        csrf_token = client.cookies["csrftoken"].value
        response = client.post(
            reverse("aist_api:auth_login"),
            data={"username": self.user.email, "password": "wrong"},
            content_type="application/json",
            HTTP_X_CSRFTOKEN=csrf_token,
        )
        self.assertEqual(response.status_code, 401)

    def test_login_by_unknown_email_rejected(self):
        client = Client(enforce_csrf_checks=True)
        client.get(reverse("client_login"))
        csrf_token = client.cookies["csrftoken"].value
        response = client.post(
            reverse("aist_api:auth_login"),
            data={"username": "nobody@example.com", "password": "pass"},
            content_type="application/json",
            HTTP_X_CSRFTOKEN=csrf_token,
        )
        self.assertEqual(response.status_code, 401)

    def test_login_by_username_still_works(self):
        # EmailBackend must only ADD email resolution, never regress the
        # existing username path (still handled by ModelBackend).
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
        # self.user is a Maintainer (AISTApiBase) -> genuinely has write access somewhere.
        self.assertTrue(response.data["can_create_write_token"])

    def test_me_get_can_create_write_token_false_for_reader_only_user(self):
        # A user whose only role anywhere is Reader must not be told they can
        # mint a read/write API token (aist.queries.user_has_write_capability
        # backs both this field and AISTApiTokenCreateSerializer.validate_scope).
        user = self.user.__class__.objects.create_user(
            username="reader-only",
            email="reader-only@example.com",
            password="pass",  # noqa: S106
        )
        client = APIClient()
        client.force_authenticate(user=user)

        pt = Product_Type.objects.create(name="Reader Only PT")
        role_reader, _ = Role.objects.get_or_create(id=Roles.Reader, defaults={"name": "Reader"})
        Product_Type_Member.objects.create(product_type=pt, user=user, role=role_reader)

        with patch("aist.api.account.get_system_setting", return_value=True):
            response = client.get(reverse("aist_api:me"))

        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.data["can_create_write_token"])

    def test_me_get_returns_distinct_roles_for_two_orgs(self):
        # self.user is already a Maintainer of self.prod_type (AISTApiBase).
        # Add a SECOND, independent org with a DIFFERENT role for the SAME
        # user — organization_memberships must report both, each with its own
        # correct role, not just the first one found or a merged/blended role.
        organization = Organization.objects.create(name="Org One")
        organization.product_type = self.prod_type
        organization.save(update_fields=["product_type"])

        pt_two = Product_Type.objects.create(name="PT Two")
        Organization.objects.create(name="Org Two", product_type=pt_two)
        product_two = Product.objects.create(
            name="Product Two", description="desc", prod_type=pt_two, sla_configuration_id=self.sla.id,
        )
        AISTProject.objects.create(
            product=product_two, supported_languages=["python"], compilable=False, profile={},
        )
        role_reader, _ = Role.objects.get_or_create(id=Roles.Reader, defaults={"name": "Reader"})
        Product_Type_Member.objects.create(product_type=pt_two, user=self.user, role=role_reader)

        with patch("aist.api.account.get_system_setting", return_value=True):
            response = self.client.get(reverse("aist_api:me"))

        self.assertEqual(response.status_code, 200)
        memberships = {m["organization_name"]: m["role_name"] for m in response.data["organization_memberships"]}
        self.assertEqual(memberships, {"Org One": "Maintainer", "Org Two": "Reader"})

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

    def test_me_patch_email_collision_rejected(self):
        # auth_user.email has no DB-level unique constraint, and this
        # serializer is the only guard against a user claiming another
        # account's email (which would then make EmailBackend login
        # ambiguous for both accounts). Case-insensitive on purpose.
        other = self.user.__class__.objects.create_user(
            username="taken-email-owner",
            email="Taken@example.com",
            password="pass",  # noqa: S106
        )
        with patch("aist.api.account.get_system_setting", return_value=True):
            response = self.client.patch(
                reverse("aist_api:me"),
                data={"email": "taken@example.com"},
                format="json",
            )
        self.assertEqual(response.status_code, 400)
        self.assertIn("email", response.json())
        self.user.refresh_from_db()
        self.assertEqual(self.user.email, "tester@example.com", "email must not have been changed")
        self.assertEqual(other.email, "Taken@example.com")

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

    def test_change_password_weak_new_password_has_readable_message(self):
        response = self.client.post(
            reverse("aist_api:me_change_password"),
            data={
                "current_password": "pass",
                "new_password": "123",
                "new_password_confirm": "123",
            },
            format="json",
        )
        self.assertEqual(response.status_code, 400)
        body = response.json()
        # PasswordChangeForm (like SetPasswordForm) validates password strength
        # in clean_new_password2, so errors land on new_password2 -> remapped
        # to new_password_confirm.
        self.assertNotIn("new_password1", body)
        self.assertNotIn("new_password2", body)
        self.assertNotIn("old_password", body)
        self.assertIn("new_password_confirm", body)
        self.assertTrue(body["new_password_confirm"])

    def test_change_password_endpoint_is_throttled(self):
        # AISTMeChangePasswordAPI now carries its own ScopedRateThrottle
        # ("aist_change_password", 10/min default) so a hijacked session
        # cannot brute-force current_password without limit.
        statuses = [
            self.client.post(
                reverse("aist_api:me_change_password"),
                data={
                    "current_password": "wrong",
                    "new_password": "N3wPass!123",
                    "new_password_confirm": "N3wPass!123",
                },
                format="json",
            ).status_code
            for _ in range(15)
        ]
        self.assertIn(429, statuses)
        self.assertTrue(all(code in {400, 429} for code in statuses))

    def test_login_and_set_password_have_independent_throttle_scopes(self):
        # AISTAuthLoginAPI uses "aist_auth_login"; AISTSetPasswordAPI now uses
        # its own "aist_auth_set_password" scope — exhausting one must not
        # 429 the other, even from the same IP.
        client = Client(enforce_csrf_checks=True)
        client.get(reverse("client_login"))
        csrf_token = client.cookies["csrftoken"].value
        login_url = reverse("aist_api:auth_login")
        for _ in range(10):
            client.post(
                login_url,
                data={"username": "nobody", "password": "wrong"},
                content_type="application/json",
                HTTP_X_CSRFTOKEN=csrf_token,
            )
        exhausted = client.post(
            login_url,
            data={"username": "nobody", "password": "wrong"},
            content_type="application/json",
            HTTP_X_CSRFTOKEN=csrf_token,
        )
        self.assertEqual(exhausted.status_code, 429)

        set_password_resp = client.post(
            reverse("aist_api:auth_set_password"),
            data={"uid": "x", "token": "y", "new_password": "a", "new_password_confirm": "a"},
            content_type="application/json",
            HTTP_X_CSRFTOKEN=csrf_token,
        )
        self.assertNotEqual(
            set_password_resp.status_code, 429,
            "login's throttle budget must not bleed into the independent set-password scope",
        )

    def test_set_password_scope_has_its_own_budget(self):
        client = Client(enforce_csrf_checks=True)
        client.get(reverse("client_login"))
        csrf_token = client.cookies["csrftoken"].value
        set_password_url = reverse("aist_api:auth_set_password")
        payload = {"uid": "x", "token": "y", "new_password": "a", "new_password_confirm": "a"}
        for _ in range(10):
            client.post(set_password_url, data=payload, content_type="application/json", HTTP_X_CSRFTOKEN=csrf_token)
        exhausted = client.post(set_password_url, data=payload, content_type="application/json", HTTP_X_CSRFTOKEN=csrf_token)
        self.assertEqual(exhausted.status_code, 429)

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


class EmailDbUniqueConstraintTests(AISTApiBase):

    """
    aist_auth_user_email_ci_unique (migration 0037) backs the app-level
    uniqueness checks with a real DB constraint, so a collision can never slip
    through even if some future code path bypasses AISTMeSerializer entirely.
    """

    def test_case_variant_duplicate_email_rejected_at_db_layer(self):
        User = get_user_model()
        User.objects.create_user("first", "shared@example.com", "pass")
        with self.assertRaises(IntegrityError), transaction.atomic():
            User.objects.create_user("second", "SHARED@Example.com", "pass")

    def test_blank_email_does_not_collide_with_itself(self):
        # The unique index is partial (WHERE email <> '') specifically because
        # multiple users legitimately have no email at all.
        User = get_user_model()
        User.objects.create_user("noemail1", "", "pass")
        User.objects.create_user("noemail2", "", "pass")
        self.assertEqual(User.objects.filter(email="").count(), 2)
