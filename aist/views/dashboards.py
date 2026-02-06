from __future__ import annotations

from django.contrib.auth.decorators import login_required
from django.http import HttpRequest, HttpResponse  # noqa: TC002
from django.shortcuts import render
from django.urls import reverse
from django.views.decorators.http import require_http_methods
from dojo.authorization.roles_permissions import Permissions
from dojo.utils import add_breadcrumb

from aist.models import AISTLaunchConfigAction, AISTStatus
from aist.queries import get_authorized_aist_organizations, get_authorized_aist_projects


@login_required
@require_http_methods(["GET"])
def launching_dashboard(request: HttpRequest) -> HttpResponse:
    """Launch Scheduling UI (read-only page; all actions go through DRF API)."""
    add_breadcrumb(title="Launch Scheduling", top_level=True, request=request)

    organizations = get_authorized_aist_organizations(Permissions.Product_View, user=request.user).order_by("name")
    projects = (
        get_authorized_aist_projects(Permissions.Product_View, user=request.user)
        .select_related("product", "organization")
        .order_by("product__name", "id")
    )

    ctx = {
        "organizations": organizations,
        "projects": projects,

        # API endpoints used by the JS on the page (single source of truth)
        "api_launch_schedules_url": reverse("aist_api:launch_schedule_list"),
        "api_project_schedule_upsert_template": reverse(
            "aist_api:project_launch_schedule_upsert",
            kwargs={"project_id": 0},
        ).replace("/0/", "/{project_id}/"),

        "api_preview_url": reverse("aist_api:launch_schedule_preview"),
        "api_queue_url": reverse("aist_api:pipeline_launch_queue_list"),
        "api_queue_clear_url": reverse("aist_api:pipeline_launch_queue_clear_dispatched"),
        "api_queue_delete_template": reverse(
            "aist_api:pipeline_launch_queue_detail",
            kwargs={"queue_id": 0},
        ).replace("/0/", "/{queue_id}/"),
        "api_bulk_disable_url": reverse("aist_api:launch_schedule_bulk_disable"),
        "api_project_launch_configs_template": reverse(
            "aist_api:project_launch_config_list_create",
            kwargs={"project_id": 0},
        ).replace("/0/", "/{project_id}/"),
        "api_launch_configs_dashboard_url": reverse("aist_api:launch_config_dashboard_list"),
        "api_launch_config_delete_template": reverse(
            "aist_api:project_launch_config_detail",
            kwargs={"project_id": 0, "config_id": 0},
        ).replace("/0/launch-configs/0/", "/{project_id}/launch-configs/{config_id}/"),
        "api_launch_config_action_detail_template": reverse(
            "aist_api:project_launch_config_action_detail",
            kwargs={"project_id": 0, "config_id": 0, "action_id": 0},
        ).replace(
            "/0/launch-configs/0/actions/0/",
            "/{project_id}/launch-configs/{config_id}/actions/{action_id}/",
        ),
        "api_launch_config_action_create_template": reverse(
            "aist_api:project_launch_config_action_list_create",
            kwargs={"project_id": 0, "config_id": 0},
        ).replace(
            "/0/launch-configs/0/actions/",
            "/{project_id}/launch-configs/{config_id}/actions/",
        ),
        "api_schedule_run_once_template": reverse(
            "aist_api:launch_schedule_run_once",
            kwargs={"launch_schedule_id": 0},
        ).replace("/0/", "/{launch_schedule_id}/"),
        "api_schedule_delete_template": reverse(
            "aist_api:launch_schedule_detail",
            kwargs={"launch_schedule_id": 0},
        ).replace("/0/", "/{launch_schedule_id}/"),
        "ui_pipeline_detail_template": reverse(
            "aist:pipeline_detail",
            kwargs={"pipeline_id": 0},
        ).replace("/0/", "/{pipeline_id}/"),
        "aist_status_choices": AISTStatus.choices,
        "aist_action_types": AISTLaunchConfigAction.ActionType.choices,
    }
    return render(request, "aist/launching/dashboard.html", ctx)
