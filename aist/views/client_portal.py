from __future__ import annotations

import json
import re
from typing import Any

from django.middleware.csrf import get_token
from django.shortcuts import render
from django.urls import reverse


def _replace_int_placeholder(url: str, name: str) -> str:
    return re.sub(r"/0(/|$)", rf"/{{{name}}}\1", url, count=1)


def _replace_str_placeholder(url: str, token: str, name: str) -> str:
    return url.replace(token, f"{{{name}}}")


def _build_routes() -> dict[str, Any]:
    return {
        "login_url": reverse("client_login"),
        "logout_url": reverse("client_logout"),
        "user_profile_url": reverse("user_profile"),
        "findings_list_url": reverse("aist_api:finding_list"),
        "finding_detail_url": _replace_int_placeholder(reverse("finding-detail", args=[0]), "id"),
        "finding_notes_url": _replace_int_placeholder(reverse("finding-notes", args=[0]), "id"),
        "test_detail_url": _replace_int_placeholder(reverse("test-detail", args=[0]), "id"),
        "engagement_detail_url": _replace_int_placeholder(reverse("engagement-detail", args=[0]), "id"),
        "projects_list_url": reverse("aist_api:project_list"),
        "project_meta_url": _replace_int_placeholder(
            reverse("aist_api:project_meta", kwargs={"project_id": 0}),
            "project_id",
        ),
        "pipelines_list_url": reverse("aist_api:pipelines"),
        "pipeline_export_url": _replace_str_placeholder(
            reverse("aist_api:pipeline_export_ai_results", kwargs={"pipeline_id": "PIPELINE_ID"}),
            "PIPELINE_ID",
            "pipeline_id",
        ),
        "finding_tags_url": reverse("aist_api:finding_tags"),
        "project_version_file_url": _replace_str_placeholder(
            _replace_int_placeholder(
                reverse(
                    "aist_api:project_version_file_blob",
                    kwargs={"project_version_id": 0, "subpath": "SUBPATH"},
                ),
                "project_version_id",
            ),
            "SUBPATH",
            "subpath",
        ),
    }


def client_portal_index(request):
    routes = _build_routes()
    csrf_token = get_token(request)
    return render(
        request,
        "aist/client_portal.html",
        {"routes_json": json.dumps(routes), "csrf_token": csrf_token},
    )
