from __future__ import annotations

from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.shortcuts import get_object_or_404
from django.views.decorators.http import require_POST

from dojo.aist.api import LaunchConfigSerializer, create_launch_config_for_project
from dojo.aist.forms import AISTLaunchConfigForm
from dojo.aist.models import AISTProject


@login_required
@require_POST
def project_launch_config_create_ui(request, project_id: int):
    project = get_object_or_404(AISTProject, pk=project_id)

    # Fixed project mode (LaunchConfig form has no "project" field by design)
    form = AISTLaunchConfigForm(request.POST, project=project)

    if not form.is_valid():
        # Return structured errors that UI can show nicely
        return JsonResponse(
            {
                "ok": False,
                "errors": form.errors,
                "non_field_errors": form.non_field_errors(),
            },
            status=400,
        )

    payload = form.to_api_create_payload(project=project)

    obj = create_launch_config_for_project(
        project=project,
        name=payload["name"],
        description=payload.get("description", ""),
        is_default=bool(payload.get("is_default", False)),
        raw_params=payload["params"],
    )

    return JsonResponse({"ok": True, "item": LaunchConfigSerializer(obj).data}, status=201)
