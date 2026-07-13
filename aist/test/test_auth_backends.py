"""
Unit tests for aist.auth_backends.EmailBackend's internal behavior.

End-to-end login-by-email behavior (success/failure over the real login
endpoint) is covered by aist.test.test_account_api.AISTAccountAPITests. This
file targets the backend's timing-side-channel countermeasure directly, which
isn't observable at the endpoint level without a flaky wall-clock assertion.
"""
from __future__ import annotations

from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase

from aist.auth_backends import EmailBackend

User = get_user_model()


class EmailBackendTimingTests(TestCase):
    def test_dummy_password_hashed_for_unknown_email(self):
        # Mirrors ModelBackend's own countermeasure (#20760): hash a dummy
        # password even when no user matches, so the code path (and thus
        # roughly the time) for "unknown email" and "wrong password for a
        # real email" don't diverge in a way that lets an attacker enumerate
        # registered emails against the login endpoint by timing.
        with patch("django.contrib.auth.base_user.AbstractBaseUser.set_password") as mock_set_password:
            result = EmailBackend().authenticate(None, username="nobody@example.com", password="whatever")  # noqa: S106

        self.assertIsNone(result)
        mock_set_password.assert_called_once_with("whatever")

    def test_dummy_password_hashed_for_ambiguous_email(self):
        # auth_user now carries a DB-level case-insensitive unique index on
        # email (aist_auth_user_email_ci_unique), so two live rows sharing an
        # email can no longer exist for NEW data — but the defensive
        # MultipleObjectsReturned branch in EmailBackend still matters (legacy
        # rows predating the constraint, or any future config without it), so
        # exercise it by mocking the lookup rather than faking real duplicate
        # rows the DB would now reject.
        with (
            patch.object(User.objects, "get", side_effect=User.MultipleObjectsReturned),
            patch("django.contrib.auth.base_user.AbstractBaseUser.set_password") as mock_set_password,
        ):
            result = EmailBackend().authenticate(None, username="dup@example.com", password="whatever")  # noqa: S106

        self.assertIsNone(result)
        mock_set_password.assert_called_once_with("whatever")

    def test_missing_credentials_short_circuit_without_query(self):
        self.assertIsNone(EmailBackend().authenticate(None, username=None, password="x"))  # noqa: S106
        self.assertIsNone(EmailBackend().authenticate(None, username="a@example.com", password=None))
