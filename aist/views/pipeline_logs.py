from __future__ import annotations

from django.contrib.auth.decorators import login_required
from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods
from dojo.authorization.roles_permissions import Permissions

from aist.api.pipelines import (
    pipeline_logs_download_response,
    pipeline_logs_full_response,
    pipeline_logs_progressive_response,
    stream_logs_sse_redis_response,
    stream_logs_sse_response,
)
from aist.queries import get_authorized_aist_pipelines
from aist.views._common import ERR_PIPELINE_NOT_FOUND


@login_required
@require_http_methods(["GET"])
def pipeline_logs_progressive(request, pipeline_id: str):
    """
    Progressive log API similar to Jenkins/GitLab.
    GET params:
    - start=<int>: byte offset to read from (default 0). Returns data from this offset to EOF.
    - tail=<int>: last N lines to return initially (ignored if start is provided).
    Response headers:
    - X-Log-Size: current file size in bytes (use as next start).
    Body:
    - plain text chunk (UTF-8).
    """
    pipeline = get_object_or_404(
        get_authorized_aist_pipelines(Permissions.Product_View, user=request.user),
        id=pipeline_id,
    )
    return pipeline_logs_progressive_response(request, pipeline)


@login_required
@require_http_methods(["GET"])
def pipeline_logs_full(request, pipeline_id: str) -> HttpResponse:
    """Return the entire log file as text/plain."""
    pipeline = get_object_or_404(
        get_authorized_aist_pipelines(Permissions.Product_View, user=request.user),
        id=pipeline_id,
    )
    return pipeline_logs_full_response(pipeline)


@login_required
@require_http_methods(["GET"])
def pipeline_logs_raw(request, pipeline_id: str) -> HttpResponse:
    """Raw log content (same as full) used by 'Copy to clipboard'."""
    return pipeline_logs_full(request, pipeline_id)


@login_required
@require_http_methods(["GET"])
def pipeline_logs_download(request, pipeline_id: str) -> HttpResponse:
    """Force download of the entire log as a .log file."""
    pipeline = get_object_or_404(
        get_authorized_aist_pipelines(Permissions.Product_View, user=request.user),
        id=pipeline_id,
    )
    return pipeline_logs_download_response(pipeline)


@csrf_exempt  # SSE does not need CSRF for GET; keep it simple
@login_required
@require_http_methods(["GET"])
def stream_logs_sse(request, pipeline_id: str):
    """Server-Sent Events endpoint that streams new log lines for a pipeline. Reads from DB; emits only new tail bytes every poll tick."""
    pipeline = get_object_or_404(
        get_authorized_aist_pipelines(Permissions.Product_View, user=request.user),
        id=pipeline_id,
    )

    return stream_logs_sse_response(pipeline)


@csrf_exempt
@login_required
@require_http_methods(["GET"])
def stream_logs_sse_redis_based(request: HttpRequest, pipeline_id: str) -> HttpResponse:
    """
    SSE endpoint for pipeline logs.
    1) Replays last N lines from Redis Stream for quick backlog.
    2) Subscribes to Redis Pub/Sub and streams new lines immediately.
    """
    pipeline = get_authorized_aist_pipelines(Permissions.Product_View, user=request.user).only("id").filter(
        id=pipeline_id,
    ).first()
    if not pipeline:
        raise Http404(ERR_PIPELINE_NOT_FOUND)
    return stream_logs_sse_redis_response(pipeline)
