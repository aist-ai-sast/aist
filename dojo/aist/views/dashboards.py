from __future__ import annotations

from django.contrib.auth.decorators import login_required
from django.http import HttpRequest, HttpResponse  # noqa: TC002
from django.shortcuts import render
from django.urls import reverse
from django.views.decorators.http import require_http_methods

from dojo.aist.models import AISTProject, Organization
from dojo.utils import add_breadcrumb


@login_required
@require_http_methods(["GET"])
def launching_dashboard(request: HttpRequest) -> HttpResponse:
    """Launch Scheduling UI (read-only page; all actions go through DRF API)."""
    add_breadcrumb(title="Launch Scheduling", top_level=True, request=request)

    organizations = Organization.objects.order_by("name")
    projects = AISTProject.objects.select_related("product", "organization").order_by("product__name", "id")

    ctx = {
        "organizations": organizations,
        "projects": projects,

        # API endpoints used by the JS on the page (single source of truth)
        "api_launch_schedules_url": reverse("dojo_aist_api:launch_schedule_list"),
        "api_project_schedule_upsert_template": reverse(
            "dojo_aist_api:project_launch_schedule_upsert",
            kwargs={"project_id": 0},
        ).replace("/0/", "/{project_id}/"),

        "api_preview_url": reverse("dojo_aist_api:launch_schedule_preview"),
        "api_queue_url": reverse("dojo_aist_api:pipeline_launch_queue_list"),
        "api_queue_clear_url": reverse("dojo_aist_api:pipeline_launch_queue_clear_dispatched"),
        "api_bulk_disable_url": reverse("dojo_aist_api:launch_schedule_bulk_disable"),
        "api_project_launch_configs_template": reverse(
            "dojo_aist_api:project_launch_config_list_create",
            kwargs={"project_id": 0},
        ).replace("/0/", "/{project_id}/"),
        "api_schedule_run_once_template": reverse(
            "dojo_aist_api:launch_schedule_run_once",
            kwargs={"launch_schedule_id": 0},
        ).replace("/0/", "/{launch_schedule_id}/"),
        "ui_pipeline_detail_template": reverse(
            "dojo_aist:pipeline_detail",
            kwargs={"pipeline_id": 0},
        ).replace("/0/", "/{pipeline_id}/"),
    }
    return render(request, "dojo/aist/launching/dashboard.html", ctx)
