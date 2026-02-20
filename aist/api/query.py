from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from django.core.exceptions import ImproperlyConfigured
from django.db.models import QuerySet
from django.shortcuts import get_object_or_404

QuerysetGetter = Callable[..., QuerySet]


@dataclass(frozen=True, slots=True)
class AuthorizedQuerysetSpec:
    getter: QuerysetGetter
    permission: str


class AuthorizedQuerySetMixin:
    authorized_queryset: AuthorizedQuerysetSpec | None = None

    def get_authorized_queryset(
        self,
        *,
        permission: str | None = None,
        getter: QuerysetGetter | None = None,
    ) -> QuerySet:
        spec = self.authorized_queryset
        resolved_getter = getter or (spec.getter if spec else None)
        resolved_permission = permission or (spec.permission if spec else None)
        if resolved_getter is None or resolved_permission is None:
            msg = "AuthorizedQuerySetMixin requires authorized_queryset or explicit getter+permission"
            raise ImproperlyConfigured(msg)
        return resolved_getter(resolved_permission, user=self.request.user)

    def get_authorized_object(self, *, permission: str | None = None, getter: QuerysetGetter | None = None, **lookup):
        return get_object_or_404(
            self.get_authorized_queryset(permission=permission, getter=getter),
            **lookup,
        )
