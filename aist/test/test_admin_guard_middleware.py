from __future__ import annotations

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.models import AnonymousUser
from django.http import HttpResponse
from django.test import TestCase
from django.test.client import RequestFactory
from django.test.utils import override_settings
from django.utils.crypto import get_random_string
from dojo.models import Product_Type
from rest_framework.authentication import BaseAuthentication
from rest_framework.authtoken.models import Token
from rest_framework.exceptions import AuthenticationFailed

from aist.models import AISTApiToken, ApiTokenScope, Organization
from aist_site.middleware import AistAdminGuardMiddleware


def _make_password() -> str:
    return get_random_string(12)


class AlwaysFailAuthentication(BaseAuthentication):
    def authenticate(self, request):
        msg = "forced failure"
        raise AuthenticationFailed(msg)


class AistAdminGuardMiddlewareTests(TestCase):
    def setUp(self):
        self.factory = RequestFactory()
        self.middleware = AistAdminGuardMiddleware(lambda _request: HttpResponse("ok"))
        product_type = Product_Type.objects.create(name=f"Admin guard token PT {self.id()}")
        self.token_organization = Organization.objects.create(
            name=f"Admin guard token org {self.id()}", product_type=product_type,
        )

    def _issue_token(self, *, user, name, scope):
        return AISTApiToken.issue(
            user=user, organization=self.token_organization, name=name, scope=scope,
        )

    def test_allows_admin_auth_pages_for_anonymous(self):
        for path in ("/aist-admin/login/", "/aist-admin/logout/"):
            with self.subTest(path=path):
                request = self.factory.get(path)
                request.user = AnonymousUser()
                response = self.middleware(request)
                self.assertEqual(response.status_code, 200)

    def test_allows_public_github_hook_for_anonymous(self):
        request = self.factory.post("/aist-admin/aist/github_hook/")
        request.user = AnonymousUser()
        response = self.middleware(request)
        self.assertEqual(response.status_code, 200)

    def test_blocks_admin_paths_for_anonymous(self):
        for path in ("/aist-admin/", "/aist-admin/some-private-page/", "/aist-admin/api"):
            with self.subTest(path=path):
                request = self.factory.get(path)
                request.user = AnonymousUser()
                response = self.middleware(request)
                self.assertEqual(response.status_code, 404)

    def test_blocks_non_superuser_ui_and_auth_pages(self):
        user = get_user_model().objects.create_user(username="client", password=_make_password())
        for path in ("/aist-admin/", "/aist-admin/login/", "/aist-admin/logout/"):
            with self.subTest(path=path):
                request = self.factory.get(path)
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

    def test_blocks_all_non_superuser_admin_api_access_regardless_of_route(self):
        # client-ui no longer calls any /aist-admin/api/* route (it has its own
        # AIST-native endpoints for every call it used to make here) — a
        # non-superuser session must be denied on ANY such route, no exceptions.
        user = get_user_model().objects.create_user(username="client_api_blocked", password=_make_password())
        routes = [
            ("get", "/aist-admin/api/v2/user_profile/"),
            ("get", "/aist-admin/api/v2/findings/1/"),
            ("patch", "/aist-admin/api/v2/findings/1/"),
            ("post", "/aist-admin/api/v2/findings/1/close/"),
            ("get", "/aist-admin/api/v2/tests/1/"),
            ("get", "/aist-admin/api/v2/engagements/1/"),
            ("get", "/aist-admin/api/v2/findings/"),
            ("post", "/aist-admin/api/v2/product_members/"),
            ("get", "/aist-admin/api/v2/product_type_members/"),
            ("get", "/aist-admin/api/v2/users/"),
            ("get", "/aist-admin/api/v2/system_settings/"),
        ]
        for method, path in routes:
            with self.subTest(method=method, path=path):
                request = getattr(self.factory, method)(path)
                request.user = user
                request.COOKIES[settings.SESSION_COOKIE_NAME] = "session"
                response = self.middleware(request)
                self.assertEqual(response.status_code, 403)

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

    def test_blocks_admin_swagger_for_non_superuser_even_with_session(self):
        user = get_user_model().objects.create_user(username="client_api_swagger", password=_make_password())
        request = self.factory.get("/aist-admin/api/v2/oa3/swagger-ui/")
        request.user = user
        request.COOKIES[settings.SESSION_COOKIE_NAME] = "session"
        response = self.middleware(request)
        self.assertEqual(response.status_code, 403)

    def test_allows_admin_swagger_for_superuser(self):
        user = get_user_model().objects.create_superuser(
            username="admin_api_swagger",
            password=_make_password(),
            email="admin_api_swagger@example.com",
        )
        request = self.factory.get("/aist-admin/api/v2/oa3/swagger-ui/")
        request.user = user
        response = self.middleware(request)
        self.assertEqual(response.status_code, 200)

    def test_blocks_api_access_for_non_superuser_with_auth_headers(self):
        user = get_user_model().objects.create_user(username="client_api_3", password=_make_password())
        for authorization in ("Token abc", "Bearer abc", "Basic Zm9vOmJhcg=="):
            with self.subTest(authorization=authorization):
                request = self.factory.get(
                    "/aist-admin/api/v2/findings/",
                    HTTP_AUTHORIZATION=authorization,
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

    def test_blocks_scoped_token_on_admin_api_for_non_superuser(self):
        user = get_user_model().objects.create_user(username="scoped_client", password=_make_password())
        _token, raw = self._issue_token(user=user, name="t", scope=ApiTokenScope.READ_WRITE)
        request = self.factory.get(
            "/aist-admin/api/v2/findings/",
            HTTP_AUTHORIZATION=f"Bearer {raw}",
        )
        request.user = AnonymousUser()
        response = self.middleware(request)
        self.assertEqual(response.status_code, 403)

    def test_blocks_scoped_token_on_admin_api_even_for_superuser_owner(self):
        # Escalation guard: a scoped token owned by a superuser must NOT unlock the
        # DefectDojo API — otherwise a read-only token would grant full admin access.
        superuser = get_user_model().objects.create_superuser(
            username="scoped_root", password=_make_password(), email="scoped_root@example.com",
        )
        _token, raw = self._issue_token(user=superuser, name="t", scope=ApiTokenScope.READ_ONLY)
        request = self.factory.get(
            "/aist-admin/api/v2/findings/",
            HTTP_AUTHORIZATION=f"Bearer {raw}",
        )
        request.user = AnonymousUser()
        request.session = {}
        response = self.middleware(request)
        self.assertEqual(response.status_code, 403)

    def test_blocks_scoped_token_on_admin_swagger(self):
        superuser = get_user_model().objects.create_superuser(
            username="scoped_swagger", password=_make_password(), email="scoped_swagger@example.com",
        )
        _token, raw = self._issue_token(user=superuser, name="t", scope=ApiTokenScope.READ_ONLY)
        request = self.factory.get(
            "/aist-admin/api/v2/oa3/swagger-ui/",
            HTTP_AUTHORIZATION=f"Bearer {raw}",
        )
        request.user = AnonymousUser()
        response = self.middleware(request)
        self.assertEqual(response.status_code, 403)

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
