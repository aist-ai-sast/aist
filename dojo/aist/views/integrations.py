from __future__ import annotations

from contextlib import suppress

import gitlab
from django.contrib.auth.decorators import login_required
from django.http import HttpRequest, JsonResponse
from django.views.decorators.http import require_POST


@login_required
@require_POST
def gitlab_projects_list(request: HttpRequest) -> JsonResponse:
    """
    Return a lightweight list of projects from a GitLab instance.

    Token and URL are NOT stored anywhere, they are used only for this request.
    """
    gitlab_url = (request.POST.get("gitlab_url") or "").strip()
    gitlab_token = (request.POST.get("gitlab_token") or "").strip()

    if not gitlab_url or not gitlab_token:
        return JsonResponse(
            {"ok": False, "error": "GitLab URL and token are required."},
            status=400,
        )

    try:
        gl = gitlab.Gitlab(gitlab_url, private_token=gitlab_token)
        # Explicit auth to fail fast if token/URL are invalid.
        gl.auth()
    except Exception:
        return JsonResponse(
            {
                "ok": False,
                "error": "Unable to authenticate with GitLab. "
                         "Please check URL and personal access token.",
            },
            status=400,
        )

    projects: list[dict] = []
    try:
        # Keep it lightweight: first page, last active projects on top.
        gl_projects = gl.projects.list(
            all=True,
            per_page=100,
            order_by="last_activity_at",
            sort="desc",
        )
    except Exception:
        return JsonResponse(
            {"ok": False, "error": "Failed to fetch projects list from GitLab."},
            status=400,
        )

    for pr in gl_projects:
        language = ""
        # Try to detect the dominant language via /projects/:id/languages
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

    return JsonResponse({"ok": True, "projects": projects})
