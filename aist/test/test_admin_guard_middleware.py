from __future__ import annotations

from django.contrib.auth import get_user_model
from django.contrib.auth.models import AnonymousUser
from django.http import HttpResponse
from django.test import TestCase
from django.test.client import RequestFactory
from django.utils.crypto import get_random_string

from aist_site.middleware import AistAdminGuardMiddleware


def _make_password() -> str:
    return get_random_string(12)


class AistAdminGuardMiddlewareTests(TestCase):
    def setUp(self):
        self.factory = RequestFactory()
        self.middleware = AistAdminGuardMiddleware(lambda _request: HttpResponse("ok"))

    def test_blocks_admin_login_without_gate(self):
        request = self.factory.get("/aist-admin/login/")
        request.user = AnonymousUser()
        response = self.middleware(request)
        self.assertEqual(response.status_code, 404)

    def test_allows_admin_login_with_gate(self):
        request = self.factory.get("/aist-admin/login/", HTTP_X_AIST_ADMIN_GATE="1")
        request.user = AnonymousUser()
        response = self.middleware(request)
        self.assertEqual(response.status_code, 200)

    def test_allows_admin_root_for_anonymous_with_gate(self):
        request = self.factory.get("/aist-admin/", HTTP_X_AIST_ADMIN_GATE="1")
        request.user = AnonymousUser()
        response = self.middleware(request)
        self.assertEqual(response.status_code, 200)

    def test_blocks_non_superuser_ui_access(self):
        user = get_user_model().objects.create_user(username="client", password=_make_password())
        request = self.factory.get("/aist-admin/", HTTP_X_AIST_ADMIN_GATE="1")
        request.user = user
        response = self.middleware(request)
        self.assertEqual(response.status_code, 404)

    def test_allows_superuser_ui_access(self):
        user = get_user_model().objects.create_superuser(
            username="admin",
            password=_make_password(),
            email="admin@example.com",
        )
        request = self.factory.get("/aist-admin/", HTTP_X_AIST_ADMIN_GATE="1")
        request.user = user
        response = self.middleware(request)
        self.assertEqual(response.status_code, 200)

    def test_allows_api_access_for_non_superuser(self):
        user = get_user_model().objects.create_user(username="client_api", password=_make_password())
        request = self.factory.get("/aist-admin/api/v2/findings/")
        request.user = user
        response = self.middleware(request)
        self.assertEqual(response.status_code, 200)
