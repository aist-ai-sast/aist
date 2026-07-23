from __future__ import annotations

from django.contrib.auth.decorators import login_required
from django.http import Http404, HttpRequest, HttpResponse, HttpResponseBadRequest, JsonResponse
from django.shortcuts import get_object_or_404, render
from django.urls import reverse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods, require_POST
from dojo.authorization.authorization import user_has_permission_or_403
from dojo.authorization.roles_permissions import Permissions
from dojo.utils import add_breadcrumb

from aist.ai_filter import get_ai_filter_reference
from aist.api.projects import (
    ProjectUpdateRequestSerializer,
    default_analyzers_payload,
    project_meta_payload,
    update_project_from_payload,
)
from aist.forms import AISTLaunchConfigForm, AISTProjectVersionForm
from aist.models import AISTLaunchConfigAction, AISTProject, AISTStatus
from aist.queries import (
    get_authorized_aist_organizations,
    get_authorized_aist_projects,
)
from aist.views._common import ERR_PROJECT_NOT_FOUND


@login_required
@require_http_methods(["GET", "POST"])
def project_version_create(request: HttpRequest, project_id: int) -> HttpResponse:
    project = get_object_or_404(
        get_authorized_aist_projects(Permissions.Product_Edit, user=request.user),
        id=project_id,
    )
    user_has_permission_or_403(request.user, project.product, Permissions.Product_Edit)

    if request.method == "GET":
        form = AISTProjectVersionForm(initial={"project": project.id})
        return render(request, "aist/_project_version_form.html", {"form": form, "project": project})

    form = AISTProjectVersionForm(request.POST, request.FILES, initial={"project": project.id})
    if form.is_valid():
        obj = form.save()  # save() sets version = sha256 for FILE_HASH automatically
        return JsonResponse({
            "ok": True,
            "version": {"id": str(obj.id), "label": str(obj)},
        },
        )

    html = render(request, "aist/_project_version_form.html", {"form": form, "project": project}).content.decode(
        "utf-8",
        )
    return JsonResponse({"ok": False, "html": html}, status=400)


@login_required
@require_POST
def default_analyzers(request):
    project_id = request.POST.get("project")
    time_class = request.POST.get("time_class_level") or "slow"
    langs = request.POST.getlist("languages") or request.POST.getlist("languages[]")

    proj = (
        get_authorized_aist_projects(Permissions.Product_View, user=request.user)
        .filter(id=project_id)
        .first()
    )
    if project_id and not proj:
        raise Http404(ERR_PROJECT_NOT_FOUND)
    proj_langs = (proj.supported_languages if proj else []) or []
    langs_union = list(set((langs or []) + proj_langs))

    payload, error = default_analyzers_payload(
        project=proj,
        project_id=project_id,
        langs=langs_union,
        time_class=time_class,
    )
    if error:
        return HttpResponseBadRequest(error)
    return JsonResponse(payload)


@csrf_exempt
@login_required
def project_meta(request, pk: int):
    try:
        p = get_authorized_aist_projects(Permissions.Product_View, user=request.user).get(pk=pk)
    except AISTProject.DoesNotExist:
        raise Http404(ERR_PROJECT_NOT_FOUND)

    return JsonResponse(project_meta_payload(p))


@login_required
@require_http_methods(["POST"])
def aist_project_update_view(request: HttpRequest, project_id: int) -> HttpResponse:
    """
    Update editable fields of a single AISTProject.

    Expected POST fields:
    - supported_languages: comma-separated string, e.g. "python, c++, java"
    - compilable: "on" / missing (checkbox)
    - profile: JSON string representing an object (optional)
    """
    project = get_object_or_404(
        get_authorized_aist_projects(Permissions.Product_Edit, user=request.user),
        id=project_id,
    )
    user_has_permission_or_403(request.user, project.product, Permissions.Product_Edit)

    serializer = ProjectUpdateRequestSerializer(data=request.POST, context={"request": request})
    if not serializer.is_valid():
        return JsonResponse({"ok": False, "errors": serializer.errors}, status=400)

    payload, errors = update_project_from_payload(project=project, payload=serializer.validated_data)
    if errors:
        if errors.get("__all__") == "config not loaded":
            return HttpResponseBadRequest("config not loaded")
        return JsonResponse({"ok": False, "errors": errors}, status=400)
    return JsonResponse({"ok": True, "project": payload})


@login_required
@require_http_methods(["GET"])
def aist_project_list_view(request: HttpRequest) -> HttpResponse:
    """
    Management screen for AISTProject objects, grouped by Organization.

    Notes:
    - Projects are grouped by the Organization that owns their Product_Type.
    - Projects whose Product_Type has no Organization are shown under "Others".
    - Only fields that are safe to edit from UI are exposed:
      * supported_languages
      * compilable
      * profile

    """
    projects = list(
        get_authorized_aist_projects(Permissions.Product_View, user=request.user)
        .select_related("product__prod_type__aist_organization", "repository")
        .order_by("product__name", "id"),
    )
    organizations = list(
        get_authorized_aist_organizations(Permissions.Product_View, user=request.user)
        .order_by("name"),
    )
    projects_by_organization = {organization.id: [] for organization in organizations}
    unassigned_projects = []
    for project in projects:
        organization_id = project.organization_id
        if organization_id in projects_by_organization:
            projects_by_organization[organization_id].append(project)
        elif organization_id is None:
            unassigned_projects.append(project)
    for organization in organizations:
        organization.aist_projects = projects_by_organization[organization.id]
    organizations_for_create = get_authorized_aist_organizations(
        Permissions.Product_Type_Add_Product,
        user=request.user,
    ).order_by("name")

    add_breadcrumb(title="AIST Projects", top_level=True, request=request)
    return render(
        request,
        "aist/projects.html",
        {
            "organizations": organizations,
            "organizations_for_create": organizations_for_create,
            "unassigned_projects": unassigned_projects,
            "launch_config_form": AISTLaunchConfigForm(),
            "aist_status_choices": AISTStatus.choices,
            "aist_action_types": AISTLaunchConfigAction.ActionType.choices,
            "ai_filter_reference": get_ai_filter_reference(),
            # URL template: replace literal "0" with a project id in JS.
            "active_script_url_template": reverse("aist_api:project_active_script", kwargs={"project_id": 0}),
            "project_scripts_url_template": reverse("aist_api:project_script_list_create", kwargs={"project_id": 0}),
            # Both 0s are replaced in JS: first for project_id, second for version_id.
            "project_version_script_url_template": reverse(
                "aist_api:project_version_script_update",
                kwargs={"project_id": 0, "version_id": 0},
            ),
        },
    )
