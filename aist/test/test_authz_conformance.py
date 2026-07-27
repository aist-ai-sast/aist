"""
Structural guard — no AIST endpoint may skip the central authorization layer.

Walks the AIST URLconf (`aist/api_urls.py`) and asserts every routed view is an
``AISTAPIView`` subclass carrying a valid ``authz`` declaration. Together with the
import-time ``__init_subclass__`` check, this makes "add a new endpoint without an
explicit access decision" impossible to merge.

The migration is complete, so this is a permanent gate rather than a temporary
expected failure.
"""
from __future__ import annotations

from django.test import SimpleTestCase
from django.urls import URLResolver

from aist import api_urls
from aist.authz import AISTAuthzMixin, ResourcePolicy, is_valid_authz

_DAST_TENANT_ROUTE_NAMES = frozenset({
    "organization_dast_integration_import",
    "dast_integration_onboarding_detail",
    "dast_integration_disable",
    "dast_integration_rotate_token",
    "organization_dast_target_catalog",
    "dast_integration_sync_capabilities",
    "project_dast_binding_list_create",
    "project_dast_binding_detail",
    "pipeline_start",
    "pipeline_stop",
    "project_launch_config_start",
    "aist_pipeline_import_validate",
    "aist_pipeline_import",
})


def _iter_view_classes(patterns):
    for pattern in patterns:
        if isinstance(pattern, URLResolver):
            yield from _iter_view_classes(pattern.url_patterns)
            continue
        callback = pattern.callback
        view_class = getattr(callback, "view_class", None) or getattr(callback, "cls", None)
        if view_class is not None:
            yield pattern, view_class


def _nonconforming_views():
    bad = []
    for pattern, view_class in _iter_view_classes(api_urls.urlpatterns):
        conforms = (
            isinstance(view_class, type)
            and issubclass(view_class, AISTAuthzMixin)
            and is_valid_authz(getattr(view_class, "authz", None))
        )
        if not conforms:
            bad.append((getattr(pattern, "name", "?"), f"{view_class.__module__}.{view_class.__name__}"))
    return bad


class AuthzConformanceTests(SimpleTestCase):

    def test_walker_finds_views(self):
        # Sanity: the enumeration actually resolves view classes from the URLconf.
        views = list(_iter_view_classes(api_urls.urlpatterns))
        self.assertGreater(len(views), 0, "URLconf walker found no AIST views")

    def test_every_aist_endpoint_uses_central_authz(self):
        bad = _nonconforming_views()
        self.assertEqual(
            bad, [],
            f"{len(bad)} AIST endpoint(s) do not use AISTAPIView + a valid authz: {bad}",
        )

    def test_dast_tenant_endpoints_cannot_use_public_authz(self):
        routed = {
            pattern.name: view_class
            for pattern, view_class in _iter_view_classes(api_urls.urlpatterns)
            if pattern.name in _DAST_TENANT_ROUTE_NAMES
        }
        self.assertEqual(
            _DAST_TENANT_ROUTE_NAMES - set(routed),
            set(),
            "The DAST authz gate must enumerate every expected tenant endpoint.",
        )
        invalid = {
            name: f"{view_class.__module__}.{view_class.__name__}"
            for name, view_class in routed.items()
            if not isinstance(getattr(view_class, "authz", None), ResourcePolicy)
        }
        self.assertEqual(
            invalid,
            {},
            "DAST tenant endpoints must use a tenant-scoped ResourcePolicy; PUBLIC and "
            f"INTERNAL_SERVICE are forbidden: {invalid}",
        )
