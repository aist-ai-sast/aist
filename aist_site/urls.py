from django.urls import include, path, re_path
from django.views.generic import RedirectView
from dojo.user.views import login_view, logout_view
from dojo.utils import get_system_setting

from aist.views.client_portal import client_portal_index
from aist_site import views as aist_site_views

urlpatterns = [
    path(
        "",
        RedirectView.as_view(pattern_name="findings", permanent=False),
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
    re_path(
        r"^{}api/v2/aist/".format(get_system_setting("url_prefix")),
        include(("aist.api_urls", "aist_api")),
        name="aist_api",
    ),
    path("auth/login/", login_view, name="client_login"),
    path("auth/logout/", logout_view, name="client_logout"),
    path("aist-admin/", include("dojo.urls")),
    re_path(r"^(?!aist-admin/|aist/|api/|projects_version/|auth/|assets/).*$", client_portal_index),
]

handler404 = aist_site_views.aist_not_found
handler403 = aist_site_views.aist_forbidden
handler500 = aist_site_views.aist_server_error
