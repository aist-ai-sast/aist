"""
Tests for the AIST-branded set-password flow (invite / reset).

The emailed link must point at the AIST client-ui page (not the vendor admin
reset page), and the anonymous set-password endpoint must validate the token.
"""
from __future__ import annotations

from django.contrib.auth import get_user_model
from django.contrib.auth.tokens import default_token_generator
from django.core import mail
from django.core.cache import cache
from django.test import TestCase
from django.urls import reverse
from django.utils.encoding import force_bytes
from django.utils.http import urlsafe_base64_encode
from rest_framework.test import APIClient

from aist.members.email import send_set_password_email

User = get_user_model()

STRONG_PASSWORD = "Str0ng!Passw0rd"  # noqa: S105  (test fixture)


class SetPasswordEndpointTests(TestCase):
    def setUp(self):
        cache.clear()  # reset the aist_auth_set_password throttle between tests
        self.user = User.objects.create_user("invitee", "invitee@example.com", "old-pass")
        self.client = APIClient()  # anonymous
        self.url = reverse("aist_api:auth_set_password")

    def _uid(self, user=None):
        return urlsafe_base64_encode(force_bytes((user or self.user).pk))

    def _token(self, user=None):
        return default_token_generator.make_token(user or self.user)

    def test_anonymous_sets_password_with_valid_token(self):
        resp = self.client.post(
            self.url,
            {
                "uid": self._uid(),
                "token": self._token(),
                "new_password": STRONG_PASSWORD,
                "new_password_confirm": STRONG_PASSWORD,
            },
            format="json",
        )
        self.assertEqual(resp.status_code, 204)
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password(STRONG_PASSWORD))

    def test_invalid_token_rejected(self):
        resp = self.client.post(
            self.url,
            {
                "uid": self._uid(),
                "token": "bogus-token",
                "new_password": STRONG_PASSWORD,
                "new_password_confirm": STRONG_PASSWORD,
            },
            format="json",
        )
        self.assertEqual(resp.status_code, 400)
        self.user.refresh_from_db()
        self.assertFalse(self.user.check_password(STRONG_PASSWORD))

    def test_weak_password_rejected_with_readable_message(self):
        resp = self.client.post(
            self.url,
            {
                "uid": self._uid(),
                "token": self._token(),
                "new_password": "123",
                "new_password_confirm": "123",
            },
            format="json",
        )
        self.assertEqual(resp.status_code, 400)
        body = resp.json()
        # Must be keyed under a serializer field name — Django's SetPasswordForm
        # attaches password-strength validator errors to new_password2 (it
        # validates in clean_new_password2, after confirming the match), which
        # never reached the frontend's error extractor, producing an opaque
        # "Request failed" instead of the actual validator requirements.
        self.assertNotIn("new_password1", body)
        self.assertNotIn("new_password2", body)
        self.assertIn("new_password_confirm", body)
        self.assertTrue(body["new_password_confirm"])
        self.assertTrue(all(isinstance(m, str) and m for m in body["new_password_confirm"]))

    def test_mismatched_passwords_rejected(self):
        resp = self.client.post(
            self.url,
            {
                "uid": self._uid(),
                "token": self._token(),
                "new_password": STRONG_PASSWORD,
                "new_password_confirm": STRONG_PASSWORD + "x",
            },
            format="json",
        )
        self.assertEqual(resp.status_code, 400)

    def test_token_single_use(self):
        payload = {
            "uid": self._uid(),
            "token": self._token(),
            "new_password": STRONG_PASSWORD,
            "new_password_confirm": STRONG_PASSWORD,
        }
        first = self.client.post(self.url, payload, format="json")
        self.assertEqual(first.status_code, 204)
        # The token is invalidated once the password changes.
        second = self.client.post(self.url, payload, format="json")
        self.assertEqual(second.status_code, 400)


class SetPasswordEmailTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("mailee", "mailee@example.com", "x")

    def test_invite_email_links_to_client_ui_not_admin(self):
        send_set_password_email(self.user, purpose="invite")
        self.assertEqual(len(mail.outbox), 1)
        message = mail.outbox[0]
        self.assertIn("/auth/set-password/", message.body)
        self.assertNotIn("/aist-admin/", message.body)
        # HTML alternative present and branded, no vendor name leaked.
        html = next(content for content, mimetype in message.alternatives if mimetype == "text/html")
        self.assertIn("/auth/set-password/", html)
        self.assertNotIn("DefectDojo", html)

    def test_reset_email_uses_reset_copy(self):
        send_set_password_email(self.user, purpose="reset")
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("Reset your", mail.outbox[0].subject)

    def test_email_has_logo_and_no_dead_box_shadow(self):
        send_set_password_email(self.user, purpose="invite")
        html = next(content for content, mimetype in mail.outbox[0].alternatives if mimetype == "text/html")
        self.assertIn("<img src=", html)
        self.assertIn("logo.jpg", html)
        self.assertNotIn("box-shadow", html)
        # Light, brand-neutral background — not the old near-black body.
        self.assertIn("#eef1f7", html)
        self.assertNotIn("#0f172a", html)
