from __future__ import annotations

import re

from django.urls import URLPattern
from rest_framework.response import Response
from rest_framework.test import APIRequestFactory, force_authenticate

from aist.api.response import MaskedAPIResponse
from aist.api_urls import urlpatterns
from aist.test.test_api import AISTApiBase

_ROUTE_ARG_RE = re.compile(r"<(?:(?P<converter>\w+):)?(?P<name>\w+)>")


def _kwargs_from_route(route: str) -> dict[str, str | int]:
    kwargs: dict[str, str | int] = {}
    for match in _ROUTE_ARG_RE.finditer(route):
        converter = match.group("converter") or "str"
        name = match.group("name")
        if converter == "int":
            kwargs[name] = 1
        elif converter == "path":
            kwargs[name] = "a/b.txt"
        else:
            kwargs[name] = "x"
    return kwargs


def _select_method(view_cls) -> str:
    for method in ("get", "post", "patch", "delete"):
        if hasattr(view_cls, method):
            return method
    return "get"


class APIResponseClassTests(AISTApiBase):
    def test_all_aist_api_patterns_return_masked_response_class_for_drf_responses(self):
        factory = APIRequestFactory()

        for pattern in urlpatterns:
            if not isinstance(pattern, URLPattern):
                continue
            view_func = pattern.callback
            view_cls = getattr(view_func, "view_class", None)
            if view_cls is None:
                continue

            method = _select_method(view_cls)
            path = f"/api/v2/aist/{pattern.pattern}"
            kwargs = _kwargs_from_route(str(pattern.pattern))

            request_builder = getattr(factory, method)
            if method in {"post", "patch"}:
                request = request_builder(path, data={}, format="json")
            else:
                request = request_builder(path)

            force_authenticate(request, user=self.user)
            response = view_func(request, **kwargs)

            if isinstance(response, Response):
                self.assertIsInstance(response, MaskedAPIResponse, msg=f"{pattern.name} should return MaskedAPIResponse")
