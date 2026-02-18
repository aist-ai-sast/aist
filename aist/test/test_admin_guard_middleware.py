from __future__ import annotations

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.models import AnonymousUser
from django.http import HttpResponse
from django.test import TestCase
from django.test.client import RequestFactory
from django.test.utils import override_settings
from django.utils.crypto import get_random_string
from rest_framework.authentication import BaseAuthentication
from rest_framework.authtoken.models import Token
from rest_framework.exceptions import AuthenticationFailed

from aist_site.middleware import AistAdminGuardMiddleware


def _make_password() -> str:
    return get_random_string(12)


class AlwaysFailAuthentication(BaseAuthentication):
    def authenticate(self, request):
        raise AuthenticationFailed("forced failure")


class AistAdminGuardMiddlewareTests(TestCase):
    def setUp(self):
        self.factory = RequestFactory()
        self.middleware = AistAdminGuardMiddleware(lambda _request: HttpResponse("ok"))

    def test_allows_admin_login_for_anonymous(self):
        request = self.factory.get("/aist-admin/login/")
        request.user = AnonymousUser()
        response = self.middleware(request)
        self.assertEqual(response.status_code, 200)

    def test_allows_admin_logout_for_anonymous(self):
        request = self.factory.get("/aist-admin/logout/")
        request.user = AnonymousUser()
        response = self.middleware(request)
        self.assertEqual(response.status_code, 200)

    def test_blocks_admin_root_for_anonymous(self):
        request = self.factory.get("/aist-admin/")
        request.user = AnonymousUser()
        response = self.middleware(request)
        self.assertEqual(response.status_code, 404)

    def test_blocks_non_login_admin_path_for_anonymous(self):
        request = self.factory.get("/aist-admin/some-private-page/")
        request.user = AnonymousUser()
        response = self.middleware(request)
        self.assertEqual(response.status_code, 404)

    def test_blocks_non_superuser_ui_access(self):
        user = get_user_model().objects.create_user(username="client", password=_make_password())
        request = self.factory.get("/aist-admin/")
        request.user = user
        response = self.middleware(request)
        self.assertEqual(response.status_code, 403)

    def test_blocks_non_superuser_login_page_access(self):
        user = get_user_model().objects.create_user(username="client_login", password=_make_password())
        request = self.factory.get("/aist-admin/login/")
        request.user = user
        response = self.middleware(request)
        self.assertEqual(response.status_code, 403)

    def test_blocks_non_superuser_logout_page_access(self):
        user = get_user_model().objects.create_user(username="client_logout", password=_make_password())
        request = self.factory.get("/aist-admin/logout/")
        request.user = user
        response = self.middleware(request)
        self.assertEqual(response.status_code, 403)

    def test_allows_superuser_ui_access(self):
        user = get_user_model().objects.create_superuser(
            username="admin",
            password=_make_password(),
            email="admin@example.com",
        )
        request = self.factory.get("/aist-admin/")
        request.user = user
        response = self.middleware(request)
        self.assertEqual(response.status_code, 200)

    def test_allows_api_access_for_non_superuser_with_session(self):
        user = get_user_model().objects.create_user(username="client_api", password=_make_password())
        request = self.factory.get("/aist-admin/api/v2/findings/")
        request.user = user
        request.COOKIES[settings.SESSION_COOKIE_NAME] = "session"
        response = self.middleware(request)
        self.assertEqual(response.status_code, 200)

    def test_allows_api_access_for_superuser(self):
        user = get_user_model().objects.create_superuser(
            username="admin_api",
            password=_make_password(),
            email="admin_api@example.com",
        )
        request = self.factory.get("/aist-admin/api/v2/findings/")
        request.user = user
        response = self.middleware(request)
        self.assertEqual(response.status_code, 200)

    def test_blocks_api_access_for_non_superuser_without_session(self):
        user = get_user_model().objects.create_user(username="client_api_2", password=_make_password())
        request = self.factory.get("/aist-admin/api/v2/findings/")
        request.user = user
        response = self.middleware(request)
        self.assertEqual(response.status_code, 403)

    def test_blocks_api_access_for_non_superuser_with_token_header(self):
        user = get_user_model().objects.create_user(username="client_api_3", password=_make_password())
        request = self.factory.get(
            "/aist-admin/api/v2/findings/",
            HTTP_AUTHORIZATION="Token abc",
        )
        request.user = user
        request.COOKIES[settings.SESSION_COOKIE_NAME] = "session"
        response = self.middleware(request)
        self.assertEqual(response.status_code, 403)

    def test_blocks_api_access_for_non_superuser_with_bearer_header(self):
        user = get_user_model().objects.create_user(username="client_api_4", password=_make_password())
        request = self.factory.get(
            "/aist-admin/api/v2/findings/",
            HTTP_AUTHORIZATION="Bearer abc",
        )
        request.user = user
        request.COOKIES[settings.SESSION_COOKIE_NAME] = "session"
        response = self.middleware(request)
        self.assertEqual(response.status_code, 403)

    def test_blocks_api_access_for_non_superuser_with_basic_header(self):
        user = get_user_model().objects.create_user(username="client_api_5", password=_make_password())
        request = self.factory.get(
            "/aist-admin/api/v2/findings/",
            HTTP_AUTHORIZATION="Basic Zm9vOmJhcg==",
        )
        request.user = user
        request.COOKIES[settings.SESSION_COOKIE_NAME] = "session"
        response = self.middleware(request)
        self.assertEqual(response.status_code, 403)

    def test_blocks_api_access_for_anonymous_even_with_session_cookie(self):
        request = self.factory.get("/aist-admin/api/v2/findings/")
        request.user = AnonymousUser()
        request.COOKIES[settings.SESSION_COOKIE_NAME] = "session"
        response = self.middleware(request)
        self.assertEqual(response.status_code, 403)

    def test_blocks_api_access_for_non_superuser_without_cookie_even_if_session_in_query(self):
        user = get_user_model().objects.create_user(username="client_api_6", password=_make_password())
        request = self.factory.get("/aist-admin/api/v2/findings/?sessionid=fake")
        request.user = user
        response = self.middleware(request)
        self.assertEqual(response.status_code, 403)

    def test_allows_api_access_for_superuser_with_authorization_header(self):
        user = get_user_model().objects.create_superuser(
            username="admin_api_auth",
            password=_make_password(),
            email="admin_api_auth@example.com",
        )
        request = self.factory.get(
            "/aist-admin/api/v2/findings/",
            HTTP_AUTHORIZATION="Bearer admin-token",
        )
        request.user = user
        response = self.middleware(request)
        self.assertEqual(response.status_code, 200)

    def test_allows_api_access_for_superuser_with_valid_token_when_request_user_anonymous(self):
        user = get_user_model().objects.create_superuser(
            username="admin_api_token",
            password=_make_password(),
            email="admin_api_token@example.com",
        )
        token = Token.objects.create(user=user)
        request = self.factory.get(
            "/aist-admin/api/v2/findings/",
            HTTP_AUTHORIZATION=f"Token {token.key}",
        )
        request.user = AnonymousUser()
        request.session = {}
        request.session = {}
        response = self.middleware(request)
        self.assertEqual(response.status_code, 200)

    def test_blocks_api_access_for_non_superuser_with_valid_token_without_session(self):
        user = get_user_model().objects.create_user(
            username="client_api_token",
            password=_make_password(),
        )
        token = Token.objects.create(user=user)
        request = self.factory.get(
            "/aist-admin/api/v2/findings/",
            HTTP_AUTHORIZATION=f"Token {token.key}",
        )
        request.user = AnonymousUser()
        response = self.middleware(request)
        self.assertEqual(response.status_code, 403)

    @override_settings(
        REST_FRAMEWORK={
            **settings.REST_FRAMEWORK,
            "DEFAULT_AUTHENTICATION_CLASSES": (
                "aist.test.test_admin_guard_middleware.AlwaysFailAuthentication",
                "rest_framework.authentication.TokenAuthentication",
            ),
        },
    )
    def test_allows_superuser_token_when_first_authenticator_fails(self):
        user = get_user_model().objects.create_superuser(
            username="admin_api_chain",
            password=_make_password(),
            email="admin_api_chain@example.com",
        )
        token = Token.objects.create(user=user)
        request = self.factory.get(
            "/aist-admin/api/v2/findings/",
            HTTP_AUTHORIZATION=f"Token {token.key}",
        )
        request.user = AnonymousUser()
        response = self.middleware(request)
        self.assertEqual(response.status_code, 200)

    def test_superuser_token_is_attached_to_request_user_for_downstream_middlewares(self):
        user = get_user_model().objects.create_superuser(
            username="admin_api_downstream",
            password=_make_password(),
            email="admin_api_downstream@example.com",
        )
        token = Token.objects.create(user=user)
        request = self.factory.get(
            "/aist-admin/api/v2/findings/",
            HTTP_AUTHORIZATION=f"Token {token.key}",
        )
        request.user = AnonymousUser()

        middleware = AistAdminGuardMiddleware(
            lambda req: HttpResponse("ok" if req.user.is_authenticated else "redirect", status=200 if req.user.is_authenticated else 302),
        )

        response = middleware(request)
        self.assertEqual(response.status_code, 200)

    def test_superuser_token_populates_request_user_and_cache(self):
        user = get_user_model().objects.create_superuser(
            username="admin_api_cache",
            password=_make_password(),
            email="admin_api_cache@example.com",
        )
        token = Token.objects.create(user=user)
        request = self.factory.get(
            "/aist-admin/api/v2/findings/",
            HTTP_AUTHORIZATION=f"Token {token.key}",
        )
        request.user = AnonymousUser()
        request.session = {}

        captured = {}

        def _view(req):
            captured["user"] = req.user
            captured["cached_user"] = getattr(req, "_cached_user", None)
            return HttpResponse("ok")

        middleware = AistAdminGuardMiddleware(_view)
        response = middleware(request)

        self.assertEqual(response.status_code, 200)
        self.assertTrue(captured["user"].is_authenticated)
        self.assertTrue(captured["user"].is_superuser)
        self.assertEqual(captured["user"].pk, user.pk)
        self.assertIsNotNone(captured["cached_user"])
        self.assertTrue(captured["cached_user"].is_superuser)
        self.assertEqual(captured["cached_user"].pk, user.pk)

    def test_allows_admin_static_for_anonymous(self):
        request = self.factory.get("/aist-admin/static/admin.css")
        request.user = AnonymousUser()
        response = self.middleware(request)
        self.assertEqual(response.status_code, 200)

    def test_path_outside_admin_prefix_is_not_restricted(self):
        request = self.factory.get("/api/v2/findings/")
        request.user = AnonymousUser()
        response = self.middleware(request)
        self.assertEqual(response.status_code, 200)

    def test_blocks_api_like_path_without_trailing_slash_for_anonymous(self):
        request = self.factory.get("/aist-admin/api")
        request.user = AnonymousUser()
        response = self.middleware(request)
        self.assertEqual(response.status_code, 404)
