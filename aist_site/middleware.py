from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING

from django.conf import settings
from django.http import JsonResponse
from django.shortcuts import render
from rest_framework import exceptions
from rest_framework.authentication import get_authorization_header
from rest_framework.request import Request
from rest_framework.settings import api_settings

from aist.findings_bulk_lock import get_locked_finding_ids
from aist.utils.secrets import mask_sensitive_data

if TYPE_CHECKING:
    from django.http import HttpResponse


class AistResponseMaskingMiddleware:
    aist_prefixes = ("/aist/", "/aist-admin/aist/")

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        if not any(request.path_info.startswith(prefix) for prefix in self.aist_prefixes):
            return response
        return self._mask_json_response(response)

    def _mask_json_response(self, response: HttpResponse) -> HttpResponse:
        if getattr(response, "streaming", False):
            return response

        content_type = (response.get("Content-Type") or "").lower()
        if "application/json" not in content_type:
            return response
        if "application/vnd.oai.openapi+json" in content_type:
            return response

        try:
            payload = json.loads(response.content.decode("utf-8"))
        except Exception:
            return response

        if isinstance(payload, dict) and "openapi" in payload and "paths" in payload and "info" in payload:
            return response

        masked = mask_sensitive_data(payload)
        if masked == payload:
            return response

        response.content = json.dumps(masked, ensure_ascii=False).encode("utf-8")
        response.headers["Content-Length"] = str(len(response.content))
        return response


class AistNoStoreHtmlMiddleware:

    """
    Ensure browser/proxy caches never store HTML responses.

    This prevents stale SPA shell pages after deploy while allowing
    static assets to be cached separately.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        if getattr(response, "streaming", False):
            return response

        content_type = (response.get("Content-Type") or "").lower()
        if "text/html" not in content_type:
            return response

        response.headers["Cache-Control"] = "no-store, max-age=0, must-revalidate"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
        return response


class AistAdminGuardMiddleware:

    """
    Security guard for all routes under `/aist-admin/`.

    Enforced restrictions:
    - `/aist-admin/static/*` is always allowed.
    - `/aist-admin/api/*`:
      - superuser: always allowed.
      - superuser authenticated via API auth header (Token/Bearer/etc): allowed.
      - non-superuser: allowed only for authenticated UI-session requests.
      - non-superuser token-style requests (`Authorization` header) are denied.
    - Other `/aist-admin/*` UI pages:
      - authenticated superuser: allowed.
      - authenticated non-superuser: denied (403).
      - anonymous users: only `/aist-admin/login/` and `/aist-admin/logout/`
        are allowed; all other paths return 404.
    """

    admin_prefix = "/aist-admin/"
    admin_api_prefix = "/aist-admin/api/"
    admin_swagger_prefixes = ("/aist-admin/api/v2/oa3/schema/", "/aist-admin/api/v2/oa3/swagger-ui/")
    admin_static_prefix = "/aist-admin/static/"
    admin_public_paths = {"/aist-admin/aist/github_hook/"}
    login_paths = {"/aist-admin/login/", "/aist-admin/logout/"}

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        path = request.path_info
        if not path.startswith(self.admin_prefix):
            return self.get_response(request)

        if path.startswith(self.admin_static_prefix):
            return self.get_response(request)

        if path in self.admin_public_paths:
            return self.get_response(request)

        if path.startswith(self.admin_api_prefix):
            return self._handle_admin_api(request)

        return self._handle_admin_ui(request)

    def _deny_not_found(self, request):
        return render(
            request,
            "aist/error.html",
            {"title": "Page Not Found", "message": "The page you're looking for is unavailable or access is restricted."},
            status=404,
        )

    def _deny_forbidden(self, request):
        return render(
            request,
            "aist/error.html",
            {"title": "Access Denied", "message": "You don't have permission to access this page."},
            status=403,
        )

    def _handle_admin_api(self, request):
        if request.path_info.startswith(self.admin_swagger_prefixes):
            return self._handle_admin_swagger(request)

        user = request.user
        if user.is_authenticated and user.is_superuser:
            return self.get_response(request)

        api_user = self._authenticate_api_header_user(request)
        if api_user is not None:
            if api_user.is_superuser:
                request.user = api_user
                request._cached_user = api_user
                return self.get_response(request)
            return self._deny_forbidden(request)

        if self._has_explicit_api_auth_header(request):
            return self._deny_forbidden(request)

        if not self._is_ui_session_user(request):
            return self._deny_forbidden(request)

        return self.get_response(request)

    def _handle_admin_swagger(self, request):
        user = request.user
        if user.is_authenticated and user.is_superuser:
            return self.get_response(request)

        api_user = self._authenticate_api_header_user(request)
        if api_user is not None and api_user.is_superuser:
            request.user = api_user
            request._cached_user = api_user
            return self.get_response(request)

        return self._deny_forbidden(request)

    def _handle_admin_ui(self, request):
        user = request.user
        if user.is_authenticated:
            if user.is_superuser:
                return self.get_response(request)
            return self._deny_forbidden(request)

        if request.path_info in self.login_paths:
            return self.get_response(request)

        return self._deny_not_found(request)

    def _is_ui_session_user(self, request) -> bool:
        user = request.user
        if not user.is_authenticated:
            return False
        return bool(request.COOKIES.get(settings.SESSION_COOKIE_NAME))

    def _has_explicit_api_auth_header(self, request) -> bool:
        return bool(get_authorization_header(request))

    def _authenticate_api_header_user(self, request):
        if not self._has_explicit_api_auth_header(request):
            return None

        drf_request = Request(request)
        for auth_cls in api_settings.DEFAULT_AUTHENTICATION_CLASSES:
            authenticator = auth_cls()
            try:
                auth_result = authenticator.authenticate(drf_request)
            except exceptions.APIException:
                continue
            if auth_result is None:
                continue
            user, _ = auth_result
            return user
        return None


class AistFindingBulkLockMiddleware:

    """
    UX guard for single-finding mutations during an active bulk status update.

    Returns HTTP 423 early to PATCH / PUT / DELETE on ``/api/v2/findings/{id}/``
    and POST to ``/api/v2/findings/{id}/close/`` while the finding is marked
    as in-flight by a bulk operation — saving the request from queuing behind
    the DB row lock and giving the user a clear actionable error.

    This is a UX optimisation, NOT a data integrity mechanism.
    Concurrent write safety is guaranteed by ``select_for_update(nowait=True)``
    inside ``AISTFindingBulkStatusAPI``.

    If the cache marker is absent (e.g. cache backend down or TTL expired),
    this middleware does nothing — the DB-level lock remains in effect.
    """

    detail_url_re = re.compile(r"^/api/v2/findings/(?P<finding_id>\d+)/?$")
    close_url_re = re.compile(r"^/api/v2/findings/(?P<finding_id>\d+)/close/?$")
    mutating_methods = {"PATCH", "PUT", "POST", "DELETE"}

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        finding_id = self._extract_finding_id(request)
        if finding_id is not None and get_locked_finding_ids([finding_id]):
            return JsonResponse(
                {
                    "detail": "Finding is locked by an active bulk status update.",
                    "finding_id": finding_id,
                },
                status=423,
            )
        return self.get_response(request)

    def _extract_finding_id(self, request) -> int | None:
        method = (request.method or "").upper()
        if method not in self.mutating_methods:
            return None

        path = request.path_info or ""
        close_match = self.close_url_re.match(path)
        if close_match and method == "POST":
            return int(close_match.group("finding_id"))

        detail_match = self.detail_url_re.match(path)
        if detail_match and method in {"PATCH", "PUT", "DELETE"}:
            return int(detail_match.group("finding_id"))
        return None
