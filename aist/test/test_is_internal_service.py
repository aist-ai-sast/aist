"""
Tests for IsInternalService (aist/authz/permissions.py).

Internal/callback endpoints accept ONLY the superuser service principal via
session/stock token; ordinary users, anonymous requests, and — crucially — any
scoped ``aistpat_`` token (even a superuser's) are denied.
"""
from __future__ import annotations

from django.contrib.auth import get_user_model
from django.contrib.auth.models import AnonymousUser
from django.test import TestCase
from rest_framework.test import APIRequestFactory

from aist.authz.permissions import IsInternalService

User = get_user_model()


class IsInternalServiceTests(TestCase):

    def setUp(self):
        self.factory = APIRequestFactory()
        self.perm = IsInternalService()

    def _request(self, **extra):
        return self.factory.post("/api/v2/aist/pipelines/x/callback/", **extra)

    def test_superuser_session_allowed(self):
        req = self._request()
        req.user = User.objects.create(username="aist-service", is_superuser=True, is_active=True)
        self.assertTrue(self.perm.has_permission(req, None))

    def test_ordinary_authenticated_user_denied(self):
        req = self._request()
        req.user = User.objects.create(username="joe", is_superuser=False, is_active=True)
        self.assertFalse(self.perm.has_permission(req, None))

    def test_anonymous_denied(self):
        req = self._request()
        req.user = AnonymousUser()
        self.assertFalse(self.perm.has_permission(req, None))

    def test_scoped_token_denied_even_for_superuser(self):
        # A scoped aistpat_ bearer is never an internal-service principal, regardless
        # of the owner being a superuser.
        req = self._request(HTTP_AUTHORIZATION="Bearer aistpat_publicid_secretsecret")
        req.user = User.objects.create(username="su2", is_superuser=True, is_active=True)
        self.assertFalse(self.perm.has_permission(req, None))
