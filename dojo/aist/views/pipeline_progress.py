from __future__ import annotations

from django.contrib.auth.decorators import login_required
from django.http import Http404, JsonResponse
from django.shortcuts import get_object_or_404
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from dojo.aist.api.pipelines import (
    deduplication_progress_payload,
    pipeline_enrich_progress_response,
    pipeline_status_stream_response,
)
from dojo.aist.models import AISTPipeline
from dojo.aist.queries import get_authorized_aist_pipelines
from dojo.authorization.roles_permissions import Permissions
from dojo.aist.views._common import ERR_PIPELINE_NOT_FOUND


@login_required
def pipeline_status_stream(request, pipeline_id: str):
    """SSE endpoint: sends 'status' on status change; finishes with 'done' on FINISHED/FAILED/DELETED."""
    # Quick existence check
    if not get_authorized_aist_pipelines(Permissions.Product_View, user=request.user).filter(id=pipeline_id).exists():
        raise Http404(ERR_PIPELINE_NOT_FOUND)

    return pipeline_status_stream_response(pipeline_id)


@login_required
def deduplication_progress_json(request, pipeline_id: str):
    """Return deduplication progress for a pipeline as JSON (per Test and overall). Progress counts findings, not just tests with a boolean flag."""
    pipeline = get_object_or_404(
        get_authorized_aist_pipelines(Permissions.Product_View, user=request.user),
        id=pipeline_id,
    )

    return JsonResponse(deduplication_progress_payload(pipeline))


@csrf_exempt
@login_required
@require_http_methods(["GET"])
def pipeline_enrich_progress_sse(request, pipeline_id: str):
    if not get_authorized_aist_pipelines(Permissions.Product_View, user=request.user).filter(id=pipeline_id).exists():
        raise Http404(ERR_PIPELINE_NOT_FOUND)
    return pipeline_enrich_progress_response(pipeline_id)
