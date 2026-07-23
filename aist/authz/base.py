"""
The single sanctioned base for every AIST API endpoint.

The enforcement lives in ``AISTAuthzMixin`` so it composes with either a plain
``APIView`` (``AISTAPIView``, the common case) or a DRF ``generics.*`` view
(``class X(AISTAuthzMixin, generics.ListAPIView)``). Either way the mixin turns
authorization from an opt-in convention into an enforced-by-construction contract:

- Every concrete subclass MUST declare ``authz`` (a ``ResourcePolicy`` or the
  ``PUBLIC``/``INTERNAL_SERVICE`` marker). A missing/invalid declaration raises
  ``ImproperlyConfigured`` **at import time**, so an unscoped endpoint cannot even
  be loaded, let alone shipped. Intermediate (non-routed) bases opt out with
  ``class Base(AISTAPIView, abstract=True)``.
- Object resolution goes through the ONE resolver ``self.resolve(...)``, which
  derives the org-scoped getter + required permission from the declared policy and
  the HTTP method — no per-view ``get_object_or_404(Model, ...)`` on org-owned data.
- ``INTERNAL_SERVICE`` endpoints get ``IsInternalService`` unconditionally; a
  subclass may not weaken it. ``PUBLIC``/``ResourcePolicy`` views default to
  ``IsAuthenticated`` and may still set ``AllowAny`` (e.g. login).

Deliberately self-contained (no ``aist.api`` import) to avoid an import cycle with
the view modules that import this base.
"""
from __future__ import annotations

from django.core.exceptions import ImproperlyConfigured
from django.shortcuts import get_object_or_404
from rest_framework.permissions import IsAuthenticated
from rest_framework.views import APIView

from aist.authz.permissions import IsInternalService
from aist.authz.policy import (
    ACTION_PERMISSIONS,
    INTERNAL_SERVICE,
    RESOURCE_GETTERS,
    Action,
    ResourcePolicy,
    is_valid_authz,
)


class AISTAuthzMixin:

    """Authorization enforcement + the sole object resolver. See module docstring."""

    permission_classes = [IsAuthenticated]

    # Subclasses MUST override with a ResourcePolicy / PUBLIC / INTERNAL_SERVICE.
    authz: object = None

    def __init_subclass__(cls, *, abstract: bool = False, **kwargs) -> None:
        super().__init_subclass__(**kwargs)
        if abstract:
            return
        authz = getattr(cls, "authz", None)
        if not is_valid_authz(authz):
            msg = (
                f"{cls.__module__}.{cls.__qualname__} must declare a valid `authz` "
                "(ResourcePolicy, PUBLIC, or INTERNAL_SERVICE). Undeclared AIST "
                "endpoints are forbidden — see aist/authz/policy.py."
            )
            raise ImproperlyConfigured(msg)
        # INTERNAL_SERVICE endpoints are service-principal only, unconditionally.
        # A subclass must NOT weaken this by declaring its own permission_classes —
        # that would silently reopen a callback to non-service callers, so it is a
        # loud import-time error rather than a quietly-honored override.
        if authz is INTERNAL_SERVICE:
            if "permission_classes" in cls.__dict__:
                msg = (
                    f"{cls.__module__}.{cls.__qualname__} declares authz=INTERNAL_SERVICE "
                    "and must NOT set its own permission_classes; IsInternalService is "
                    "enforced by the base."
                )
                raise ImproperlyConfigured(msg)
            cls.permission_classes = [IsInternalService]

    def resolve(self, *, action: Action | None = None, resource=None, **lookup):
        """
        Fetch a single org-owned object for this request, tenant-scoped.

        Picks the getter + permission from ``self.authz`` and the request method
        (safe method → read action, else write action). 404s on a cross-tenant or
        unknown identifier — the sole sanctioned object lookup for AIST resources.
        """
        if not isinstance(self.authz, ResourcePolicy):
            msg = "resolve() requires a ResourcePolicy `authz`; this view has none."
            raise ImproperlyConfigured(msg)
        queryset = self.authorized_queryset(action=action, resource=resource)
        return get_object_or_404(queryset, **lookup)

    def authorized_queryset(self, *, action: Action | None = None, resource=None):
        """Return a tenant-scoped queryset using only named, centrally mapped actions."""
        if not isinstance(self.authz, ResourcePolicy):
            msg = "authorized_queryset_for_request() requires a ResourcePolicy `authz`."
            raise ImproperlyConfigured(msg)
        resolved_resource = resource or self.authz.resource
        try:
            getter = RESOURCE_GETTERS[resolved_resource]
        except KeyError as exc:
            msg = f"No org-scoped getter registered for {resolved_resource.__name__}"
            raise ImproperlyConfigured(msg) from exc
        permission = (
            ACTION_PERMISSIONS[action]
            if action is not None
            else self.authz.permission_for(self.request.method)
        )
        return getter(permission, user=self.request.user)

    def authorized_queryset_for_request(self):
        """Backward-compatible spelling used by the first migration batch."""
        return self.authorized_queryset()


class AISTAPIView(AISTAuthzMixin, APIView, abstract=True):

    """Enforced base for plain (non-generics) AIST endpoints."""
