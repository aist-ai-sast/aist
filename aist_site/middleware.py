from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING

from django.http import JsonResponse
from django.shortcuts import render
from django.urls import Resolver404, resolve
from rest_framework import exceptions
from rest_framework.authentication import get_authorization_header
from rest_framework.request import Request
from rest_framework.settings import api_settings

from aist.authentication import header_carries_scoped_token, resolve_scoped_token
from aist.findings_bulk_lock import get_locked_finding_ids
from aist.models import ApiTokenScope
from aist.utils.secrets import is_openapi_payload, mask_sensitive_data, view_disables_masking

if TYPE_CHECKING:
    from django.http import HttpResponse


class AistResponseMaskingMiddleware:

    """
    Masks secret-looking values in JSON responses from plain (non-DRF) AIST views
    under ``/aist/`` and ``/aist-admin/aist/`` (defense-in-depth). DRF API views
    under ``/api/v2/aist/`` are masked separately by
    ``aist.api.response.install_masked_api_response`` (patched onto
    ``APIView.finalize_response``, since it needs access to the DRF response data
    before rendering) — the two layers cover disjoint URL domains by design, not
    duplicate coverage of the same requests.

    A view that INTENTIONALLY returns a secret exactly once (e.g. the one-time
    reveal of a freshly created API token) opts out by declaring
    ``disable_response_masking = True`` on the view class — otherwise the secret
    would be turned into ``********`` and be useless to the caller. Both masking
    layers consult the same ``aist.utils.secrets.view_disables_masking`` check so
    they can never disagree about which views opted out.
    """

    aist_prefixes = ("/aist/", "/aist-admin/aist/")

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        if not any(request.path_info.startswith(prefix) for prefix in self.aist_prefixes):
            return response
        if view_disables_masking(request):
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

        if is_openapi_payload(payload):
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
      - superuser: always allowed, on any route (no path restriction).
      - superuser authenticated via API auth header (Token/Bearer/etc): allowed.
      - non-superuser: always denied, regardless of session or route. This
        vendor API surface (`dojo.urls`, mounted here) was never designed
        with AIST's own business rules (org isolation, role caps, etc.) in
        mind. client-ui now has its own AIST-native endpoints for every call
        it used to make here (`aist/views/client_portal.py`'s route map
        points at `aist_api:` names exclusively), so no session-based
        non-superuser path onto the vendor API is legitimate any more.
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
        # AIST scoped tokens (aistpat_...) are NEVER valid on the DefectDojo API,
        # even if their owner is a superuser — a scoped token must not be an
        # escalation path into /aist-admin/. Deny before any other check.
        if header_carries_scoped_token(request):
            return self._deny_forbidden(request)

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

        return self._deny_forbidden(request)

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


class AistTokenScopeMiddleware:

    """
    Enforces read-only vs read-write scope for AIST personal access tokens.

    Generic and endpoint-declared, with NO hardcoded paths:

    - Default: safe methods (GET/HEAD/OPTIONS) are reads; everything else is a
      write. New endpoints are covered automatically.
    - An endpoint that performs a READ through a write method (e.g. an export or
      preview implemented as POST) opts in by setting the class attribute
      ``token_read_only = True`` on its view — one declaration on the endpoint,
      no logic duplication and no path matching here.

    So a ``read_only`` token can call every read (GET, plus endpoint-declared
    read-POSTs) and is denied only on genuine writes. (Finer per-endpoint
    permission checks belong in the endpoints as the role model evolves; this
    middleware only enforces the token's own scope.)

    It lives in middleware, not a DRF permission, because AIST views set their own
    ``permission_classes`` (which would replace a process-wide default permission).
    Session and stock-token requests carry no ``aistpat_`` token and are unaffected.
    """

    aist_api_marker = "/api/v2/aist/"
    safe_methods = frozenset({"GET", "HEAD", "OPTIONS"})

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if self._is_read_only_token_write(request):
            return JsonResponse({"detail": "This token is read-only."}, status=403)
        return self.get_response(request)

    def _is_read_only_token_write(self, request) -> bool:
        if request.method in self.safe_methods:
            return False
        if self.aist_api_marker not in request.path_info:
            return False
        if self._endpoint_declares_read_only(request):
            return False
        token = resolve_scoped_token(request)
        return token is not None and token.scope != ApiTokenScope.READ_WRITE

    @staticmethod
    def _endpoint_declares_read_only(request) -> bool:
        """True if the target view marks itself a read operation (``token_read_only``)."""
        try:
            match = resolve(request.path_info)
        except Resolver404:
            return False
        view_class = getattr(match.func, "view_class", None)
        return bool(getattr(view_class, "token_read_only", False))


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
