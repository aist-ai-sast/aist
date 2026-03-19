from __future__ import annotations

from django.contrib.auth.decorators import login_required
from django.http import HttpRequest, JsonResponse
from django.views.decorators.http import require_POST
from dojo.authorization.roles_permissions import Permissions

from aist.api.integrations import gitlab_projects_list_payload
from aist.models import OrgIntegration
from aist.queries import get_authorized_aist_organizations


@login_required
@require_POST
def gitlab_projects_list(request: HttpRequest) -> JsonResponse:
    """
    Return a lightweight list of projects from a GitLab instance.

    Requires ``organization_id``: resolves the default active GitLab integration
    for that organization and uses its stored credentials.
    """
    organization_id = (request.POST.get("organization_id") or "").strip()
    if not organization_id:
        return JsonResponse({"ok": False, "error": "organization_id is required."}, status=400)

    accessible_orgs = get_authorized_aist_organizations(Permissions.Product_View, user=request.user)
    if not accessible_orgs.filter(pk=organization_id).exists():
        return JsonResponse({"ok": False, "error": "Organization not found."}, status=404)

    integration = (
        OrgIntegration.objects
        .filter(organization_id=organization_id, integration_type="GITLAB", is_active=True)
        .order_by("pk")
        .first()
    )
    if not integration:
        return JsonResponse(
            {"ok": False, "error": "No active GitLab integration found for this organization."},
            status=404,
        )

    url = (integration.config or {}).get("base_url", "").strip()
    token = (integration.secret or "").strip()

    payload, status_code = gitlab_projects_list_payload(url, token)
    return JsonResponse(payload, status=status_code)
