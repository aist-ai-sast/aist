from __future__ import annotations

from django.contrib.auth.decorators import login_required
from django.http import HttpRequest, JsonResponse
from django.views.decorators.http import require_POST

from aist.api.integrations import gitlab_projects_list_payload

@login_required
@require_POST
def gitlab_projects_list(request: HttpRequest) -> JsonResponse:
    """
    Return a lightweight list of projects from a GitLab instance.

    Token and URL are NOT stored anywhere, they are used only for this request.
    """
    payload, status = gitlab_projects_list_payload(
        (request.POST.get("gitlab_url") or "").strip(),
        (request.POST.get("gitlab_token") or "").strip(),
    )
    return JsonResponse(payload, status=status)
