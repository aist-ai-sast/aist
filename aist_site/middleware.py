from __future__ import annotations

from django.http import HttpResponseForbidden, HttpResponseNotFound


class AistAdminGuardMiddleware:
    """Block non-superusers from DefectDojo UI while keeping API access intact."""

    admin_prefix = "/aist-admin/"

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        path = request.path_info
        if path.startswith(self.admin_prefix):
            if path.startswith("/aist-admin/api/") or path.startswith("/aist-admin/static/"):
                return self.get_response(request)

            if request.headers.get("X-Aist-Admin-Gate") != "1":
                return HttpResponseNotFound("Not Found")

            if path in ("/aist-admin/login/", "/aist-admin/logout/"):
                return self.get_response(request)

            user = request.user
            if user.is_authenticated and not user.is_superuser:
                return HttpResponseForbidden("Forbidden")
            return self.get_response(request)

        return self.get_response(request)
