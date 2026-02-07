from django.urls import include, path, re_path
from dojo.utils import get_system_setting

from dojo.user.views import login_view, logout_view

from aist.views.client_portal import client_portal_index

urlpatterns = [
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
