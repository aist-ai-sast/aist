from django.urls import include, path, re_path
from django.views.generic import RedirectView
from django_github_app.views import AsyncWebhookView
from dojo.user.views import logout_view
from dojo.utils import get_system_setting

from aist.views.auth import logout_all_devices_view
from aist.views.client_portal import client_portal_index
from aist.views.summaries import dashboard_summary, pipeline_summary, product_summary
from aist_site import views as aist_site_views
from aist_site.openapi import build_schema_custom_settings

urlpatterns = [
    path(
        "",
        RedirectView.as_view(url="/dashboard", permanent=False),
    ),
    path(
        "dashboard",
        client_portal_index,
        name="dashboard",
    ),
    path(
        "findings",
        client_portal_index,
        name="findings",
    ),
    path(
        "findings/",
        client_portal_index,
    ),
    path("aist-admin/aist/", include(("aist.urls", "aist"), namespace="aist")),
    path("aist/github_hook/", AsyncWebhookView.as_view(), name="aist_github_hook_public"),
    path("summary/products/", product_summary, name="client_product_summary"),
    path("summary/pipelines/", pipeline_summary, name="client_pipeline_summary"),
    path("summary/dashboard/", dashboard_summary, name="client_dashboard_summary"),
    re_path(
        r"^{}api/v2/aist/".format(get_system_setting("url_prefix")),
        include(("aist.api_urls", "aist_api")),
        name="aist_api",
    ),
    path("auth/login/", client_portal_index, name="client_login"),
    # Anonymous set-password page (emailed invite/reset link) — served by the SPA.
    re_path(r"^auth/set-password/[^/]+/[^/]+/?$", client_portal_index, name="client_set_password"),
    path("auth/logout/", logout_view, name="client_logout"),
    path("auth/logout-all/", logout_all_devices_view, name="client_logout_all_devices"),
    path(
        "aist-admin/api/v2/oa3/swagger-ui/",
        RedirectView.as_view(url="/aist-admin/api/v2/oa3/swagger-ui/aist/", permanent=False),
        name="admin_swagger_ui_oa3_root",
    ),
    path(
        "aist-admin/api/v2/oa3/swagger-ui/aist/",
        aist_site_views.AistOnlySpectacularSwaggerView.as_view(
            url="/aist-admin/api/v2/oa3/schema/?format=json",
        ),
        name="admin_swagger_ui_oa3_aist",
    ),
    path(
        "aist-admin/api/v2/oa3/swagger-ui/aist/dojo/",
        RedirectView.as_view(url="/aist-admin/api/v2/oa3/swagger-ui/dojo/", permanent=False),
        name="admin_swagger_ui_oa3_aist_dojo_redirect",
    ),
    path(
        "aist-admin/api/v2/oa3/swagger-ui/dojo/",
        aist_site_views.AistOnlySpectacularSwaggerView.as_view(
            url="/aist-admin/api/v2/oa3/schema/dojo/?format=json",
        ),
        name="admin_swagger_ui_oa3_dojo",
    ),
    path(
        "aist-admin/api/v2/oa3/schema/",
        aist_site_views.AistOnlySpectacularAPIView.as_view(
            custom_settings=build_schema_custom_settings(
                preprocessing_hook="aist_site.views.aist_only_preprocessing_hook",
            ),
        ),
        name="admin_schema_oa3_aist",
    ),
    path(
        "aist-admin/api/v2/oa3/schema/dojo/",
        aist_site_views.AistOnlySpectacularAPIView.as_view(
            custom_settings=build_schema_custom_settings(
                preprocessing_hook="aist_site.views.dojo_preprocessing_hook",
            ),
        ),
        name="admin_schema_oa3_dojo",
    ),
    path("aist-admin/", include("dojo.urls")),
    path(
        "api/v2/oa3/schema/",
        aist_site_views.AistOnlySpectacularAPIView.as_view(
            custom_settings=build_schema_custom_settings(
                preprocessing_hook="aist_site.views.aist_only_preprocessing_hook",
            ),
        ),
    ),
    path(
        "api/v2/oa3/swagger-ui/",
        aist_site_views.AistOnlySpectacularSwaggerView.as_view(
            url="/api/v2/oa3/schema/?format=json",
        ),
        name="swagger-ui_oa3_aist",
    ),
    path(
        "api/v2/oa3/schema/dojo/",
        aist_site_views.AistOnlySpectacularAPIView.as_view(
            custom_settings=build_schema_custom_settings(
                preprocessing_hook="aist_site.views.dojo_preprocessing_hook",
            ),
        ),
        name="schema_oa3_dojo",
    ),
    path(
        "api/v2/oa3/swagger-ui/dojo/",
        aist_site_views.AistOnlySpectacularSwaggerView.as_view(
            url="/api/v2/oa3/schema/dojo/?format=json",
        ),
        name="swagger-ui_oa3_dojo",
    ),
    re_path(r"^(?!aist-admin/|aist/|api/|projects_version/|auth/|assets/).*$", client_portal_index),
]

handler404 = aist_site_views.aist_not_found
handler403 = aist_site_views.aist_forbidden
handler500 = aist_site_views.aist_server_error
