from __future__ import annotations

import csv
import json
import pathlib
import time
from contextlib import suppress
from io import BytesIO, StringIO

from django.db import close_old_connections, transaction
from django.db.models import Count
from django.http import HttpResponse, HttpResponseBadRequest, StreamingHttpResponse
from django.shortcuts import get_object_or_404
from drf_spectacular.utils import OpenApiExample, OpenApiParameter, OpenApiResponse, extend_schema
from openpyxl import Workbook
from rest_framework import generics, serializers, status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.settings import api_settings
from rest_framework.views import APIView

from dojo.aist.ai_filter import validate_and_normalize_filter
from dojo.aist.api.bootstrap import _import_sast_pipeline_package  # noqa: F401
from dojo.aist.logging_transport import BACKLOG_COUNT, PUBSUB_CHANNEL_TPL, STREAM_KEY, get_pipeline_log_path, get_redis
from dojo.aist.models import AISTPipeline, AISTProjectVersion, AISTStatus, TestDeduplicationProgress
from dojo.aist.queries import get_authorized_aist_pipelines, get_authorized_aist_project_versions
from dojo.authorization.authorization import user_has_permission_or_403
from dojo.authorization.roles_permissions import Permissions
from dojo.aist.pipeline_args import PipelineArguments
from dojo.aist.tasks import run_sast_pipeline
from dojo.aist.utils.export import _build_ai_export_rows
from dojo.aist.utils.pipeline import create_pipeline_object, has_unfinished_pipeline, stop_pipeline


class PipelineStartRequestSerializer(serializers.Serializer):
    project_version_id = serializers.IntegerField(required=True)
    ai_filter = serializers.JSONField(required=False, allow_null=True)


class PipelineResponseSerializer(serializers.Serializer):
    id = serializers.CharField()
    status = serializers.CharField()
    response_from_ai = serializers.JSONField(allow_null=True)
    created = serializers.DateTimeField()
    updated = serializers.DateTimeField()


class PipelineStartAPI(APIView):

    """Start a new AIST pipeline."""

    permission_classes = [IsAuthenticated]

    @extend_schema(
        request=PipelineStartRequestSerializer,
        responses={
            201: OpenApiResponse(PipelineResponseSerializer, description="Pipeline created"),
            404: OpenApiResponse(description="Project version not found"),
            405: OpenApiResponse(description="There is already a running pipeline for this project version"),
        },
        examples=[
            OpenApiExample(
                "Start by version id",
                value={
                    "limit": 50,
                    "project_version_id": 123,
                    "ai_filter": {"severity": [
                        {"comparison": "EQUALS", "value": "High"},
                        {"comparison": "EQUALS", "value": "Critical"},
                    ]},
                },
                request_only=True,
            ),
        ],
        tags=["aist"],
        summary="Start pipeline",
        description="Creates and starts AIST Pipeline for the given existing AISTProjectVersion.",
    )
    def post(self, request, *args, **kwargs) -> Response:
        if api_settings.URL_FORMAT_OVERRIDE:
            setattr(request, api_settings.URL_FORMAT_OVERRIDE, None)

        # validate body
        serializer = PipelineStartRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        pv_id = serializer.validated_data["project_version_id"]
        # we take project from version to avoid double inputs
        project_version = get_object_or_404(
            get_authorized_aist_project_versions(Permissions.Product_Edit, user=request.user),
            pk=pv_id,
        )
        project = project_version.project
        user_has_permission_or_403(request.user, project.product, Permissions.Product_Edit)
        provided_ai_filter = serializer.validated_data.get("ai_filter", None)

        if has_unfinished_pipeline(project_version):
            return Response(status=status.HTTP_405_METHOD_NOT_ALLOWED)

        if not provided_ai_filter:
            return Response(
                {"ai_filter": "ai_filter is required for AUTO_DEFAULT"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            normalized_filter = validate_and_normalize_filter(provided_ai_filter)
        except Exception as e:
            return Response(
                {"ai_filter": str(e)},
                status=status.HTTP_400_BAD_REQUEST,
            )

        raw = {
            "ai_mode": "AUTO_DEFAULT",
            "ai_filter_snapshot": normalized_filter,
            # keep API behavior stable: no analyzers override here (same as before)
            "analyzers": [],
            "selected_languages": [],
            "rebuild_images": False,
            "log_level": "INFO",
            "time_class_level": None,
            "project_version": project_version.as_dict(),
        }

        params = PipelineArguments.normalize_params(project=project, raw_params=raw)

        # create pipeline in transaction
        with transaction.atomic():
            p = create_pipeline_object(project, project_version, None)

        async_result = run_sast_pipeline.delay(p.id, params)
        p.run_task_id = async_result.id
        p.save(update_fields=["run_task_id"])

        out = PipelineResponseSerializer(
            {"id": p.id, "status": p.status, "response_from_ai": p.response_from_ai, "created": p.created,
             "updated": p.updated})
        return Response(out.data, status=status.HTTP_201_CREATED)


class PipelineListAPI(generics.ListAPIView):

    """Paginated list of pipelines with simple filtering."""

    permission_classes = [IsAuthenticated]
    serializer_class = PipelineResponseSerializer

    @extend_schema(
        tags=["aist"],
        summary="List pipelines",
        description=(
            "Returns a paginated list of AIST pipelines. "
            "Filters: project_id, status, created_gte/lte (ISO8601). "
            "Ordering: created, -created, updated, -updated."
        ),
        parameters=[
            OpenApiParameter(name="project_id", location=OpenApiParameter.QUERY, description="Filter by AISTProject id", required=False, type=int),
            OpenApiParameter(name="status", location=OpenApiParameter.QUERY, description="Filter by status (string/choice)", required=False, type=str),
            OpenApiParameter(name="created_gte", location=OpenApiParameter.QUERY, description="Created >= (ISO8601)", required=False, type=str),
            OpenApiParameter(name="created_lte", location=OpenApiParameter.QUERY, description="Created <= (ISO8601)", required=False, type=str),
            OpenApiParameter(name="ordering", location=OpenApiParameter.QUERY, description="created | -created | updated | -updated", required=False, type=str),
            # Pagination params from LimitOffsetPagination:
            OpenApiParameter(name="limit", location=OpenApiParameter.QUERY, required=False, type=int),
            OpenApiParameter(name="offset", location=OpenApiParameter.QUERY, required=False, type=int),
        ],
        responses={200: PipelineResponseSerializer(many=True)},
    )
    def get(self, request, *args, **kwargs):
        return super().get(request, *args, **kwargs)

    def get_queryset(self):
        qs = (
            get_authorized_aist_pipelines(Permissions.Product_View, user=self.request.user)
            .select_related("project", "project_version")
            .order_by("-created")
        )
        qp = self.request.query_params

        project_id = qp.get("project_id")
        status = qp.get("status")
        created_gte = qp.get("created_gte")
        created_lte = qp.get("created_lte")
        ordering = qp.get("ordering")

        if project_id:
            qs = qs.filter(project_id=project_id)
        if status:
            qs = qs.filter(status=status)
        if created_gte:
            qs = qs.filter(created__gte=created_gte)
        if created_lte:
            qs = qs.filter(created__lte=created_lte)
        if ordering in {"created", "-created", "updated", "-updated"}:
            qs = qs.order_by(ordering)

        return qs


class PipelineAPI(APIView):

    """Retrieve or delete a pipeline by id."""

    permission_classes = [IsAuthenticated]

    @extend_schema(
        responses={200: PipelineResponseSerializer, 404: OpenApiResponse(description="Not found")},
        tags=["aist"],
        summary="Get pipeline status",
        description="Returns pipeline status and AI response.",
    )
    def get(self, request, pipeline_id: str, *args, **kwargs) -> Response:
        p = get_object_or_404(
            get_authorized_aist_pipelines(Permissions.Product_View, user=request.user),
            id=pipeline_id,
        )
        data = {
            "id": p.id,
            "status": p.status,
            "response_from_ai": p.response_from_ai,
            "created": p.created,
            "updated": p.updated,
        }
        out = PipelineResponseSerializer(data)
        return Response(out.data, status=status.HTTP_200_OK)

    @extend_schema(
        responses={204: OpenApiResponse(description="Pipeline deleted"),
                   400: OpenApiResponse(description="Cannot delete pipeline"),
                   404: OpenApiResponse(description="Not found")},
        tags=["aist"],
        summary="Delete pipeline",
        description="Deletes the specified AISTPipeline by id.",
    )
    def delete(self, request, pipeline_id: str, *args, **kwargs) -> Response:
        p = get_object_or_404(
            get_authorized_aist_pipelines(Permissions.Product_Edit, user=request.user),
            id=pipeline_id,
        )
        user_has_permission_or_403(request.user, p.project.product, Permissions.Product_Edit)
        if p.status != AISTStatus.FINISHED:
            return Response(status=status.HTTP_400_BAD_REQUEST)
        p.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


def export_ai_results_response(request, pipeline: AISTPipeline) -> HttpResponse:
    ai_response = pipeline.ai_responses.order_by("-created").first()
    if not ai_response or not ai_response.payload:
        return HttpResponseBadRequest("No AI responses available for export.")

    payload = ai_response.payload or {}
    data = getattr(request, "data", None)
    fmt = ((data.get("format") if data is not None else None) or (request.POST.get("format") if hasattr(request, "POST") else None) or "csv").lower()

    selected_columns = []
    if data is not None and hasattr(data, "getlist"):
        selected_columns = data.getlist("columns")
    if not selected_columns:
        selected_columns = request.POST.getlist("columns") if hasattr(request, "POST") else []
    if not selected_columns:
        selected_columns = [
            "title",
            "project_version",
            "cwe",
            "file",
            "line",
            "description",
            "code_snippet",
        ]

    ignore_fp = ((data.get("ignore_false_positives") if data is not None else None) or (request.POST.get("ignore_false_positives") if hasattr(request, "POST") else None) or "1").lower() in {
        "1",
        "on",
        "true",
        "yes",
    }
    export_all = ((data.get("export_all") if data is not None else None) or (request.POST.get("export_all") if hasattr(request, "POST") else None) or "").lower() in {
        "1",
        "on",
        "true",
        "yes",
    }

    max_findings_raw = ((data.get("max_findings") if data is not None else None) or (request.POST.get("max_findings") if hasattr(request, "POST") else None) or "").strip()
    try:
        max_findings_val = int(max_findings_raw) if max_findings_raw else None
        max_findings = max_findings_val if max_findings_val and max_findings_val > 0 else None
    except ValueError:
        max_findings = None

    rows = _build_ai_export_rows(pipeline, payload, ignore_false_positives=ignore_fp)
    if not rows:
        return HttpResponseBadRequest("No findings matched the selected filters.")

    if not export_all and max_findings is not None:
        rows = rows[:max_findings]

    if not ignore_fp and "false_positive" not in selected_columns:
        selected_columns.append("false_positive")

    valid_columns = {
        "title",
        "project_version",
        "cwe",
        "file",
        "line",
        "description",
        "code_snippet",
        "false_positive",
    }
    final_columns: list[str] = []
    seen: set[str] = set()
    for col in selected_columns:
        if col in valid_columns and col not in seen:
            seen.add(col)
            final_columns.append(col)

    if not final_columns:
        final_columns = ["title", "project_version", "cwe", "file", "line"]

    header_map = {
        "title": "Title",
        "project_version": "Project version",
        "cwe": "CWE",
        "file": "File",
        "line": "Line",
        "description": "Description",
        "code_snippet": "Code snippet",
        "false_positive": "False positive",
    }

    if fmt in {"xlsx", "excel", "xls"}:
        wb = Workbook()
        ws = wb.active
        ws.title = "AI results"
        ws.append([header_map[c] for c in final_columns])
        for row in rows:
            ws.append([row.get(c, "") for c in final_columns])
        buffer = BytesIO()
        wb.save(buffer)
        resp = HttpResponse(
            buffer.getvalue(),
            content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
        resp["Content-Disposition"] = f'attachment; filename="aist_ai_results_{pipeline.id}.xlsx"'
        return resp

    if fmt != "csv":
        return HttpResponseBadRequest(f"Unsupported export format: {fmt}")

    buffer = StringIO()
    writer = csv.writer(buffer)
    writer.writerow([header_map[c] for c in final_columns])
    for row in rows:
        writer.writerow([row.get(c, "") for c in final_columns])

    resp = HttpResponse(buffer.getvalue(), content_type="text/csv; charset=utf-8")
    resp["Content-Disposition"] = f'attachment; filename="aist_ai_results_{pipeline.id}.csv"'
    return resp


def pipeline_logs_progressive_response(request, pipeline: AISTPipeline) -> HttpResponse:
    path = get_pipeline_log_path(pipeline.id)
    data = ""
    size = 0
    start = request.GET.get("start")
    tail = request.GET.get("tail")

    try:
        start = int(start) if start is not None else None
    except ValueError:
        start = None
    try:
        tail = max(0, int(tail)) if tail is not None else None
    except ValueError:
        tail = None

    if pathlib.Path(path).exists():
        size = pathlib.Path(path).stat().st_size
        if tail:
            with pathlib.Path(path).open("rb") as f:
                lines = f.readlines()[-tail:]
            decoded = [ln.decode("utf-8", errors="ignore").rstrip("\r\n") for ln in lines]
            data = "\n".join(decoded)
        elif start is not None:
            start = max(0, min(start, size))
            with pathlib.Path(path).open("rb") as f:
                f.seek(start)
                chunk = f.read()
            data = chunk.decode("utf-8", errors="ignore")
        else:
            data = pathlib.Path(path).read_text(encoding="utf-8", errors="ignore")

    resp = HttpResponse(data, content_type="text/plain; charset=utf-8")
    resp["X-Log-Size"] = str(size)
    return resp


def pipeline_logs_full_response(pipeline: AISTPipeline) -> HttpResponse:
    path = get_pipeline_log_path(pipeline.id)
    content = pathlib.Path(path).read_text(encoding="utf-8", errors="ignore") if pathlib.Path(path).exists() else ""
    return HttpResponse(content, content_type="text/plain; charset=utf-8")


def pipeline_logs_download_response(pipeline: AISTPipeline) -> HttpResponse:
    resp = pipeline_logs_full_response(pipeline)
    resp["Content-Disposition"] = f'attachment; filename="pipeline-{pipeline.id}.log"'
    return resp


def stream_logs_sse_response(pipeline: AISTPipeline) -> StreamingHttpResponse:
    def event_stream():
        last_len = 0
        for _ in range(60 * 60 * 12):
            p = AISTPipeline.objects.filter(id=pipeline.id).only("logs", "status").first()
            if not p:
                break
            data = p.logs or ""
            if len(data) > last_len:
                chunk = data[last_len:]
                last_len = len(data)
                yield f"data: {chunk}\n\n"
            if p.status == AISTStatus.FINISHED:
                yield "event: done\ndata: FINISHED\n\n"
                break
            time.sleep(0.3)

    resp = StreamingHttpResponse(event_stream(), content_type="text/event-stream; charset=utf-8")
    resp["Cache-Control"] = "no-cache, no-transform"
    resp["X-Accel-Buffering"] = "no"
    return resp


def stream_logs_sse_redis_response(pipeline: AISTPipeline) -> StreamingHttpResponse:
    r = get_redis()
    channel = PUBSUB_CHANNEL_TPL.format(pipeline_id=pipeline.id)

    def _sse_data(payload: str) -> bytes:
        return f"data: {payload}\n\n".encode()

    def _sse_comment(comment: str) -> bytes:
        return f": {comment}\n\n".encode()

    def _stream_last_lines_from_redis_stream(limit: int):
        try:
            entries = r.xrevrange(STREAM_KEY, max="+", min="-", count=limit) or []
            for _entry_id, fields in reversed(entries):  # B007
                pid = (fields or {}).get("pipeline_id")
                msg = (fields or {}).get("message")
                lvl = (fields or {}).get("level")
                if not pid or pid != pipeline.id or not msg:
                    continue
                line = f"{lvl} {msg}" if lvl else msg
                yield _sse_data(line)
        except Exception:
            return

    def event_stream():
        yield from _stream_last_lines_from_redis_stream(BACKLOG_COUNT)
        pubsub = r.pubsub()
        pubsub.subscribe(channel)

        last_ping = time.monotonic()
        try:
            yield _sse_comment("connected")
            for msg in pubsub.listen():
                now = time.monotonic()
                if now - last_ping > 25:
                    yield _sse_comment("ping")
                    last_ping = now
                if msg.get("type") != "message":
                    continue
                try:
                    data = json.loads(msg["data"])
                    txt = f'{data.get("level") or ""} {data.get("message") or ""}'.strip()
                    if txt:
                        yield _sse_data(txt)
                except Exception:
                    raw = msg.get("data")
                    if isinstance(raw, str) and raw:
                        yield _sse_data(raw)
        finally:
            with suppress(Exception):
                pubsub.unsubscribe(channel)
                pubsub.close()

    resp = StreamingHttpResponse(event_stream(), content_type="text/event-stream")
    resp["Cache-Control"] = "no-cache"
    resp["X-Accel-Buffering"] = "no"
    return resp


def pipeline_status_stream_response(pipeline_id: str) -> StreamingHttpResponse:
    def event_stream():
        last_status = None
        last_updated = None
        done_at = None
        heartbeat_every = 3
        last_heartbeat = 0.0

        try:
            while True:
                close_old_connections()
                obj = (
                    AISTPipeline.objects
                    .only("id", "status", "updated")
                    .filter(id=pipeline_id)
                    .first()
                )

                if obj is None:
                    yield "event: done\ndata: deleted\n\n"
                    break

                if obj.status != last_status:
                    last_status = obj.status
                    last_updated = obj.updated
                    yield f"event: status\ndata: {last_status}\n\n"
                    if last_status in {AISTStatus.FINISHED, getattr(AISTStatus, "FAILED", "FAILED")}:
                        done_at = time.time() + 6
                elif last_status is not None and obj.updated != last_updated:
                    last_updated = obj.updated
                    yield f"event: status\ndata: {last_status}\n\n"

                now_ts = time.time()
                if now_ts - last_heartbeat >= heartbeat_every:
                    last_heartbeat = now_ts
                    yield f": heartbeat {int(now_ts)}\n\n"

                if done_at and now_ts >= done_at:
                    yield "event: done\ndata: finished\n\n"
                    break

                time.sleep(1)
        finally:
            close_old_connections()

    resp = StreamingHttpResponse(event_stream(), content_type="text/event-stream")
    resp["Cache-Control"] = "no-cache"
    resp["X-Accel-Buffering"] = "no"
    return resp


def deduplication_progress_payload(pipeline: AISTPipeline) -> dict:
    tests = (
        pipeline.tests
        .select_related("engagement")
        .annotate(total_findings=Count("finding", distinct=True))
        .order_by("id")
    )

    tests_payload = []
    overall_total = 0
    overall_processed = 0

    for t in tests:
        prog, _ = TestDeduplicationProgress.objects.get_or_create(test=t)
        prog.refresh_pending_tasks()

        total = getattr(t, "total_findings", 0)
        pending = prog.pending_tasks
        processed = max(total - pending, 0)
        pct = 100 if total == 0 else int(processed * 100 / total)

        overall_total += total
        overall_processed += processed

        tests_payload.append(
            {
                "test_id": t.id,
                "test_name": getattr(t, "title", None) or f"Test #{t.id}",
                "total_findings": total,
                "processed": processed,
                "pending": pending,
                "percent": pct,
                "completed": bool(prog.deduplication_complete),
            },
        )

    overall_pct = 100 if overall_total == 0 else int(overall_processed * 100 / overall_total)
    return {
        "status": pipeline.status,
        "overall": {
            "total_findings": overall_total,
            "processed": overall_processed,
            "pending": max(overall_total - overall_processed, 0),
            "percent": overall_pct,
        },
        "tests": tests_payload,
    }


def pipeline_enrich_progress_response(pipeline_id: str) -> StreamingHttpResponse:
    redis = get_redis()
    key = f"aist:progress:{pipeline_id}:enrich"

    def event_stream():
        last = None
        last_ping = time.monotonic()
        while True:
            try:
                total, done = redis.hmget(key, "total", "done")
            except Exception:
                total, done = 0, 0
            total = int(total or 0)
            done = int(done or 0)

            payload = {
                "total": total,
                "done": done,
                "percent": (100 if total == 0 else int(done * 100 / total)),
            }

            now = (payload["total"], payload["done"])
            if now != last:
                yield f"data: {json.dumps(payload)}\n\n"
                last = now

            if time.monotonic() - last_ping > 25:
                yield ": ping\n\n"
                last_ping = time.monotonic()

            if total and done >= total:
                yield "event: done\ndata: ok\n\n"
                break

            time.sleep(1)

    resp = StreamingHttpResponse(event_stream(), content_type="text/event-stream")
    resp["Cache-Control"] = "no-cache"
    resp["X-Accel-Buffering"] = "no"
    return resp


class PipelineStopAPI(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(responses={200: OpenApiResponse(description="Pipeline stopped")})
    def post(self, request, pipeline_id: str):
        pipeline = get_object_or_404(
            get_authorized_aist_pipelines(Permissions.Product_Edit, user=request.user),
            id=pipeline_id,
        )
        user_has_permission_or_403(request.user, pipeline.project.product, Permissions.Product_Edit)
        stop_pipeline(pipeline)
        return Response({"ok": True})


class ExportAIResultsAPI(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(responses={200: OpenApiResponse(description="Export file")})
    def post(self, request, pipeline_id: str):
        pipeline = get_object_or_404(
            get_authorized_aist_pipelines(Permissions.Product_View, user=request.user),
            id=pipeline_id,
        )
        return export_ai_results_response(request, pipeline)


class PipelineLogsProgressiveAPI(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(responses={200: OpenApiResponse(description="Log chunk")})
    def get(self, request, pipeline_id: str):
        pipeline = get_object_or_404(
            get_authorized_aist_pipelines(Permissions.Product_View, user=request.user),
            id=pipeline_id,
        )
        return pipeline_logs_progressive_response(request, pipeline)


class PipelineLogsFullAPI(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(responses={200: OpenApiResponse(description="Full log")})

    def get(self, request, pipeline_id: str):
        pipeline = get_object_or_404(
            get_authorized_aist_pipelines(Permissions.Product_View, user=request.user),
            id=pipeline_id,
        )
        return pipeline_logs_full_response(pipeline)


class PipelineLogsDownloadAPI(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(responses={200: OpenApiResponse(description="Log download")})

    def get(self, request, pipeline_id: str):
        pipeline = get_object_or_404(
            get_authorized_aist_pipelines(Permissions.Product_View, user=request.user),
            id=pipeline_id,
        )
        return pipeline_logs_download_response(pipeline)


class PipelineLogsStreamAPI(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(responses={200: OpenApiResponse(description="SSE stream")})

    def get(self, request, pipeline_id: str):
        pipeline = get_object_or_404(
            get_authorized_aist_pipelines(Permissions.Product_View, user=request.user),
            id=pipeline_id,
        )
        return stream_logs_sse_response(pipeline)


class PipelineLogsStreamRedisAPI(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(responses={200: OpenApiResponse(description="SSE stream (redis)")})

    def get(self, request, pipeline_id: str):
        pipeline = get_authorized_aist_pipelines(Permissions.Product_View, user=request.user).only("id").filter(
            id=pipeline_id,
        ).first()
        if not pipeline:
            return Response({"detail": "Pipeline not found"}, status=status.HTTP_404_NOT_FOUND)
        return stream_logs_sse_redis_response(pipeline)


class PipelineStatusStreamAPI(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(responses={200: OpenApiResponse(description="Status SSE stream")})

    def get(self, request, pipeline_id: str):
        if not get_authorized_aist_pipelines(Permissions.Product_View, user=request.user).filter(id=pipeline_id).exists():
            return Response({"detail": "Pipeline not found"}, status=status.HTTP_404_NOT_FOUND)
        return pipeline_status_stream_response(pipeline_id)


class PipelineDeduplicationProgressAPI(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(responses={200: OpenApiResponse(description="Deduplication progress")})

    def get(self, request, pipeline_id: str):
        pipeline = get_object_or_404(
            get_authorized_aist_pipelines(Permissions.Product_View, user=request.user),
            id=pipeline_id,
        )
        return Response(deduplication_progress_payload(pipeline))


class PipelineEnrichProgressAPI(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(responses={200: OpenApiResponse(description="Enrichment SSE stream")})

    def get(self, request, pipeline_id: str):
        if not get_authorized_aist_pipelines(Permissions.Product_View, user=request.user).filter(id=pipeline_id).exists():
            return Response({"detail": "Pipeline not found"}, status=status.HTTP_404_NOT_FOUND)
        return pipeline_enrich_progress_response(pipeline_id)
