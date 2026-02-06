from __future__ import annotations

from contextlib import suppress

import gitlab
from drf_spectacular.utils import OpenApiResponse, extend_schema
from rest_framework import serializers
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView


def gitlab_projects_list_payload(gitlab_url: str, gitlab_token: str) -> tuple[dict, int]:
    if not gitlab_url or not gitlab_token:
        return {"ok": False, "error": "GitLab URL and token are required."}, 400

    try:
        gl = gitlab.Gitlab(gitlab_url, private_token=gitlab_token)
        gl.auth()
    except Exception:
        return {
            "ok": False,
            "error": "Unable to authenticate with GitLab. Please check URL and personal access token.",
        }, 400

    projects: list[dict] = []
    try:
        gl_projects = gl.projects.list(
            all=True,
            per_page=100,
            order_by="last_activity_at",
            sort="desc",
        )
    except Exception:
        return {"ok": False, "error": "Failed to fetch projects list from GitLab."}, 400

    for pr in gl_projects:
        language = ""
        with suppress(Exception):
            langs = pr.languages()
            if isinstance(langs, dict) and langs:
                language = max(langs, key=langs.get)

        projects.append(
            {
                "id": pr.id,
                "name": getattr(pr, "name", "") or "",
                "path_with_namespace": getattr(pr, "path_with_namespace", "") or "",
                "description": getattr(pr, "description", "") or "",
                "web_url": getattr(pr, "web_url", "") or "",
                "default_branch": getattr(pr, "default_branch", "") or "",
                "visibility": getattr(pr, "visibility", "") or "",
                "language": language or "",
            },
        )

    return {"ok": True, "projects": projects}, 200


class GitlabProjectsListAPI(APIView):
    permission_classes = [IsAuthenticated]

    class RequestSerializer(serializers.Serializer):
        gitlab_url = serializers.CharField()
        gitlab_token = serializers.CharField()

    @extend_schema(
        request=RequestSerializer,
        responses={200: OpenApiResponse(description="GitLab projects list")},
    )
    def post(self, request):
        gitlab_url = (request.data.get("gitlab_url") or "").strip()
        gitlab_token = (request.data.get("gitlab_token") or "").strip()
        payload, status = gitlab_projects_list_payload(gitlab_url, gitlab_token)
        return Response(payload, status=status)
