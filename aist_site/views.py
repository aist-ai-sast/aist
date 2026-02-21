from __future__ import annotations

from django.http import HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import render
from drf_spectacular.views import SpectacularAPIView, SpectacularSwaggerView
from rest_framework.permissions import BasePermission


def _is_api_request(request: HttpRequest) -> bool:
    path = request.path_info
    if path.startswith(("/aist-admin/api/", "/aist-admin/api/v2/", "/api/", "/api/v2/", "/projects_version/")):
        return True
    accept = request.headers.get("Accept", "")
    return "application/json" in accept


def aist_not_found(request: HttpRequest, exception: Exception | None = None) -> HttpResponse:
    if _is_api_request(request):
        return JsonResponse({"detail": "Not found"}, status=404)
    return render(
        request,
        "aist/error.html",
        {"title": "Page Not Found", "message": "The page you're looking for is unavailable or access is restricted."},
        status=404,
    )


def aist_forbidden(request: HttpRequest, exception: Exception | None = None) -> HttpResponse:
    if _is_api_request(request):
        return JsonResponse({"detail": "Forbidden"}, status=403)
    return render(
        request,
        "aist/error.html",
        {"title": "Access Denied", "message": "You don't have permission to access this page."},
        status=403,
    )


def aist_server_error(request: HttpRequest) -> HttpResponse:
    if _is_api_request(request):
        return JsonResponse({"detail": "Server error"}, status=500)
    return render(
        request,
        "aist/error.html",
        {"title": "Something went wrong", "message": "An unexpected error occurred. Please try again later."},
        status=500,
    )


def aist_only_preprocessing_hook(endpoints):
    filtered = []
    for endpoint in endpoints:
        path, path_regex, method, callback = endpoint
        if path.startswith(("/api/v2/aist/", "/projects_version/")):
            filtered.append((path, path_regex, method, callback))
    return filtered


class IsSuperuserOnly(BasePermission):
    def has_permission(self, request, view):
        user = request.user
        return bool(user and user.is_authenticated and user.is_superuser)


class AistOnlySpectacularAPIView(SpectacularAPIView):
    permission_classes = [IsSuperuserOnly]


class AistOnlySpectacularSwaggerView(SpectacularSwaggerView):
    permission_classes = [IsSuperuserOnly]
