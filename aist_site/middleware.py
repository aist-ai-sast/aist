from __future__ import annotations

from django.conf import settings
from django.shortcuts import render


class AistAdminGuardMiddleware:

    """
    Security guard for all routes under `/aist-admin/`.

    Enforced restrictions:
    - `/aist-admin/static/*` is always allowed.
    - `/aist-admin/api/*`:
      - superuser: always allowed.
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
    admin_static_prefix = "/aist-admin/static/"
    login_paths = {"/aist-admin/login/", "/aist-admin/logout/"}

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        path = request.path_info
        if not path.startswith(self.admin_prefix):
            return self.get_response(request)

        if path.startswith(self.admin_static_prefix):
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
        user = request.user
        if user.is_authenticated and user.is_superuser:
            return self.get_response(request)

        if not self._is_ui_session_user(request):
            return self._deny_forbidden(request)

        if self._has_explicit_api_auth_header(request):
            return self._deny_forbidden(request)

        return self.get_response(request)

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
        # Token/Bearer-style auth is intentionally not accepted here for
        # non-superusers; `/aist-admin/api/*` is reserved for UI-session flow.
        return bool(request.headers.get("Authorization"))
