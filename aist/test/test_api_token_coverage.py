"""
Dynamic coverage guarantees for AIST personal access tokens.

Endpoints are discovered from the URLconf / router at runtime (NOT hardcoded), so
these tests keep holding as endpoints are added:

- Every AIST API endpoint accepts a scoped token (ScopedTokenAuthentication is in
  effect), with one documented exception (the superuser token overview).
- No vendor admin API endpoint accepts a scoped token — the admin guard denies
  every ``/aist-admin/api/v2/...`` route for an ``aistpat_`` token.
"""
from __future__ import annotations

from django.contrib.auth import get_user_model
from django.http import HttpResponse
from django.test import TestCase
from django.test.client import RequestFactory

from aist import api_urls
from aist.api.tokens import AISTAdminApiTokenListAPI
from aist.authentication import ScopedTokenAuthentication
from aist.models import AISTApiToken, ApiTokenScope
from aist_site.middleware import AistAdminGuardMiddleware

User = get_user_model()

# Endpoints intentionally NOT reachable by a scoped token (documented).
_SCOPED_TOKEN_EXEMPT = {AISTAdminApiTokenListAPI}


def _iter_aist_view_classes():
    seen = set()
    for pattern in api_urls.urlpatterns:
        view_class = getattr(pattern.callback, "cls", None) or getattr(pattern.callback, "view_class", None)
        if view_class is not None and view_class not in seen:
            seen.add(view_class)
            yield view_class


class AistApiTokenAcceptanceTests(TestCase):
    def test_every_aist_endpoint_accepts_scoped_token(self):
        view_classes = list(_iter_aist_view_classes())
        self.assertGreater(len(view_classes), 5, "URLconf discovery returned too few views")
        missing = []
        for view_class in view_classes:
            authenticators = view_class().get_authenticators()
            has_scoped = any(isinstance(auth, ScopedTokenAuthentication) for auth in authenticators)
            if not has_scoped and view_class not in _SCOPED_TOKEN_EXEMPT:
                missing.append(view_class.__name__)
        self.assertEqual(
            missing, [],
            f"These AIST endpoints do not accept scoped tokens (add ScopedTokenAuthentication "
            f"or document an exemption): {missing}",
        )


class DojoApiRejectsScopedTokenTests(TestCase):
    def setUp(self):
        self.factory = RequestFactory()
        self.middleware = AistAdminGuardMiddleware(lambda _request: HttpResponse("ok"))
        user = User.objects.create_user("scoped", "scoped@example.com", "x")
        _token, self.raw = AISTApiToken.issue(user=user, name="t", scope=ApiTokenScope.READ_WRITE)

    def test_no_vendor_api_endpoint_accepts_scoped_token(self):
        from dojo.urls import v2_api  # noqa: PLC0415

        prefixes = [prefix for prefix, _viewset, _basename in v2_api.registry]
        self.assertGreater(len(prefixes), 10, "Router discovery returned too few endpoints")
        for prefix in prefixes:
            path = f"/aist-admin/api/v2/{prefix}/"
            request = self.factory.get(path, HTTP_AUTHORIZATION=f"Bearer {self.raw}")
            from django.contrib.auth.models import AnonymousUser  # noqa: PLC0415

            request.user = AnonymousUser()
            response = self.middleware(request)
            self.assertEqual(
                response.status_code, 403,
                f"Scoped token was NOT denied on vendor endpoint /aist-admin/api/v2/{prefix}/",
            )
