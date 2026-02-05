from django.urls import include, path, re_path

from dojo.utils import get_system_setting

urlpatterns = [
    path("", include("dojo.urls")),
    path("aist/", include(("aist.urls", "aist"), namespace="aist")),
    re_path(
        r"^{}api/v2/aist/".format(get_system_setting("url_prefix")),
        include(("aist.api_urls", "aist_api")),
        name="aist_api",
    ),
]
