"""
DRF permissions for AIST's internal (non-tenant) endpoints.

``IsInternalService`` gates callback/internal endpoints (AI triage callbacks,
pipeline source-info) that are driven by the platform's own service principal —
the superuser ``aist-service`` account whose stock DRF ``Token`` is minted by
``bootstrap_service_token`` from ``AIST_SERVICE_TOKEN``. Callers "just use the
superuser token" (session or stock ``Token``); a scoped ``aistpat_`` personal
access token is NEVER an internal-service principal and is rejected outright, so
a rank-and-file token can never reach these endpoints even if its owner is a
superuser.
"""
from __future__ import annotations

from rest_framework.permissions import BasePermission

from aist.authentication import header_carries_scoped_token


class IsInternalService(BasePermission):

    """Allow only a superuser authenticated by session/stock token, never a scoped PAT."""

    message = "This endpoint is restricted to the internal service principal."

    def has_permission(self, request, view) -> bool:
        # A scoped aistpat_ token is disqualified regardless of its owner's rights.
        if header_carries_scoped_token(request):
            return False
        user = request.user
        return bool(user and user.is_authenticated and user.is_superuser)
