from __future__ import annotations

import csv
import json
import pathlib
import time
from contextlib import suppress
from dataclasses import dataclass
from io import BytesIO, StringIO
from pathlib import Path

from django.db import close_old_connections, transaction
from django.db.models import Count
from django.http import HttpResponse, HttpResponseBadRequest, StreamingHttpResponse
from django.shortcuts import get_object_or_404
from django_filters import rest_framework as django_filters
from dojo.authorization.authorization import user_has_permission_or_403
from dojo.authorization.roles_permissions import Permissions
from drf_spectacular.utils import OpenApiExample, OpenApiParameter, OpenApiResponse, extend_schema
from openpyxl import Workbook
from rest_framework import generics, serializers, status
from rest_framework.exceptions import ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.settings import api_settings
from rest_framework.views import APIView

from aist.ai_filter import validate_and_normalize_filter
from aist.api.bootstrap import _import_sast_pipeline_package  # noqa: F401
from aist.api.query import AuthorizedQuerySetMixin, AuthorizedQuerysetSpec
from aist.api.schema import AISTApiTag
from aist.launch_data import PipelineLaunchData
from aist.logging_transport import BACKLOG_COUNT, PUBSUB_CHANNEL_TPL, STREAM_KEY, get_pipeline_log_path, get_redis
from aist.models import AISTPipeline, AISTProjectVersion, AISTStatus, TestDeduplicationProgress
from aist.pipeline_args import PipelineArguments
from aist.queries import get_authorized_aist_pipelines, get_authorized_aist_project_versions
from aist.tasks import run_sast_pipeline
from aist.utils.export import _build_ai_export_rows
from aist.utils.pipeline import (
    create_pipeline_object,
    has_unfinished_pipeline,
    is_terminal_pipeline_status,
    stop_pipeline,
)


@dataclass(frozen=True, slots=True)
class PipelineApiChoices:
    status: list[str]
    ordering: list[str]


PIPELINE_API_CHOICES = PipelineApiChoices(
    status=[status for status, _label in AISTStatus.choices],
    ordering=["created", "-created", "updated", "-updated"],
)


class PipelineStartRequestSerializer(serializers.Serializer):
    project_version_id = serializers.PrimaryKeyRelatedField(
        queryset=AISTProjectVersion.objects.none(),
        write_only=True,
    )
    ai_filter = serializers.JSONField(required=False, allow_null=True)

    def get_fields(self):
        fields = super().get_fields()
        request = self.context.get("request")
        if request and getattr(request, "user", None) and request.user.is_authenticated:
            fields["project_version_id"].queryset = get_authorized_aist_project_versions(
                Permissions.Product_Edit,
                user=request.user,
            )
        return fields


class PipelineResponseSerializer(serializers.Serializer):
    id = serializers.CharField()
    status = serializers.CharField()
    response_from_ai = serializers.JSONField(allow_null=True)
    created = serializers.DateTimeField()
    updated = serializers.DateTimeField()


class PipelineStopResponseSerializer(serializers.Serializer):
    ok = serializers.BooleanField()


class ExportAIResultsRequestSerializer(serializers.Serializer):
    format = serializers.ChoiceField(choices=["csv", "xlsx"], required=False)
    columns = serializers.ListField(child=serializers.CharField(), required=False)
    ignore_false_positives = serializers.BooleanField(required=False, default=True)
    export_all = serializers.BooleanField(required=False, default=False)
    max_findings = serializers.IntegerField(required=False, min_value=1, allow_null=True)

    def to_internal_value(self, data):
        mutable = data.copy() if hasattr(data, "copy") else dict(data)
        if hasattr(mutable, "getlist"):
            list_columns = [token.strip() for token in mutable.getlist("columns") if token.strip()]
            if len(list_columns) > 1:
                mutable.setlist("columns", list_columns)
            elif len(list_columns) == 1 and "," in list_columns[0]:
                mutable.setlist("columns", [token.strip() for token in list_columns[0].split(",") if token.strip()])
        else:
            columns = mutable.get("columns")
            if isinstance(columns, str):
                mutable["columns"] = [token.strip() for token in columns.split(",") if token.strip()]
        return super().to_internal_value(mutable)


class PipelineLogsProgressiveQuerySerializer(serializers.Serializer):
    start = serializers.IntegerField(required=False, min_value=0)
    tail = serializers.IntegerField(required=False, min_value=0)


class PipelineStartAPI(AuthorizedQuerySetMixin, APIView):

    """Start a new AIST pipeline."""

    permission_classes = [IsAuthenticated]
    authorized_queryset = AuthorizedQuerysetSpec(
        getter=get_authorized_aist_project_versions,
        permission=Permissions.Product_Edit,
    )

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
        tags=[AISTApiTag.PIPELINES.value],
        summary="Start pipeline",
        description="Creates and starts AIST Pipeline for the given existing AISTProjectVersion.",
    )
    def post(self, request, *args, **kwargs) -> Response:
        if api_settings.URL_FORMAT_OVERRIDE:
            setattr(request, api_settings.URL_FORMAT_OVERRIDE, None)

        # validate body
        serializer = PipelineStartRequestSerializer(data=request.data, context={"request": request})
        serializer.is_valid(raise_exception=True)
        project_version = serializer.validated_data["project_version_id"]
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


class PipelineListAPI(AuthorizedQuerySetMixin, generics.ListAPIView):

    """Paginated list of pipelines with simple filtering."""

    permission_classes = [IsAuthenticated]
    serializer_class = PipelineResponseSerializer
    authorized_queryset = AuthorizedQuerysetSpec(
        getter=get_authorized_aist_pipelines,
        permission=Permissions.Product_View,
    )

    class FilterSet(django_filters.FilterSet):
        project_id = django_filters.NumberFilter(field_name="project_id")
        status = django_filters.ChoiceFilter(field_name="status", choices=AISTStatus.choices)
        created_gte = django_filters.IsoDateTimeFilter(field_name="created", lookup_expr="gte")
        created_lte = django_filters.IsoDateTimeFilter(field_name="created", lookup_expr="lte")
        ordering = django_filters.OrderingFilter(
            fields=(
                ("created", "created"),
                ("updated", "updated"),
            ),
        )

        class Meta:
            model = AISTPipeline
            fields = ("project_id", "status", "created_gte", "created_lte", "ordering")

    @extend_schema(
        tags=[AISTApiTag.PIPELINES.value],
        summary="List pipelines",
        description=(
            "Returns a paginated list of AIST pipelines. "
            "Filters: project_id, status, created_gte/lte (ISO8601). "
            "Ordering: created, -created, updated, -updated."
        ),
        parameters=[
            OpenApiParameter(name="project_id", location=OpenApiParameter.QUERY, description="Filter by AISTProject id", required=False, type=int),
            OpenApiParameter(name="status", location=OpenApiParameter.QUERY, required=False, type=str, enum=PIPELINE_API_CHOICES.status),
            OpenApiParameter(name="created_gte", location=OpenApiParameter.QUERY, description="Created >= (ISO8601)", required=False, type=str),
            OpenApiParameter(name="created_lte", location=OpenApiParameter.QUERY, description="Created <= (ISO8601)", required=False, type=str),
            OpenApiParameter(name="ordering", location=OpenApiParameter.QUERY, required=False, type=str, enum=PIPELINE_API_CHOICES.ordering),
            # Pagination params from LimitOffsetPagination:
            OpenApiParameter(name="limit", location=OpenApiParameter.QUERY, required=False, type=int),
            OpenApiParameter(name="offset", location=OpenApiParameter.QUERY, required=False, type=int),
        ],
        responses={200: PipelineResponseSerializer(many=True)},
    )
    def get(self, request, *args, **kwargs):
        return super().get(request, *args, **kwargs)

    def get_queryset(self):
        filterset = self.FilterSet(
            data=self.request.query_params,
            queryset=(
                self.get_authorized_queryset()
                .select_related("project", "project_version")
                .order_by("-created")
            ),
            request=self.request,
        )
        if not filterset.is_valid():
            raise ValidationError(filterset.errors)
        return filterset.qs


class PipelineAPI(AuthorizedQuerySetMixin, APIView):

    """Retrieve or delete a pipeline by id."""

    permission_classes = [IsAuthenticated]
    authorized_queryset = AuthorizedQuerysetSpec(
        getter=get_authorized_aist_pipelines,
        permission=Permissions.Product_View,
    )

    @extend_schema(
        responses={200: PipelineResponseSerializer, 404: OpenApiResponse(description="Not found")},
        tags=[AISTApiTag.PIPELINES.value],
        summary="Get pipeline status",
        description="Returns pipeline status and AI response.",
    )
    def get(self, request, pipeline_id: str, *args, **kwargs) -> Response:
        p = self.get_authorized_object(id=pipeline_id)
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
        tags=[AISTApiTag.PIPELINES.value],
        summary="Delete pipeline",
        description="Deletes the specified AISTPipeline by id.",
    )
    def delete(self, request, pipeline_id: str, *args, **kwargs) -> Response:
        p = self.get_authorized_object(permission=Permissions.Product_Edit, id=pipeline_id)
        user_has_permission_or_403(request.user, p.project.product, Permissions.Product_Edit)
        if not is_terminal_pipeline_status(p.status):
            return Response(status=status.HTTP_400_BAD_REQUEST)
        p.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


def export_ai_results_response(*, pipeline: AISTPipeline, params: dict) -> HttpResponse:
    ai_response = pipeline.ai_responses.order_by("-created").first()
    if not ai_response or not ai_response.payload:
        return HttpResponseBadRequest("No AI responses available for export.")

    payload = ai_response.payload or {}
    fmt = (params.get("format") or "csv").lower()
    selected_columns = list(params.get("columns") or [])
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

    ignore_fp = bool(params.get("ignore_false_positives", True))
    export_all = bool(params.get("export_all"))
    max_findings = params.get("max_findings")

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


def pipeline_logs_progressive_response(*, pipeline: AISTPipeline, start: int | None, tail: int | None) -> HttpResponse:
    path = get_pipeline_log_path(pipeline.id)
    data = ""
    size = 0
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
            if is_terminal_pipeline_status(p.status):
                yield f"event: done\ndata: {p.status}\n\n"
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
                    if last_status in {
                        AISTStatus.FINISHED,
                        AISTStatus.FINISHED_WITH_WARNINGS,
                        getattr(AISTStatus, "FAILED", "FAILED"),
                    }:
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


class PipelineStopAPI(AuthorizedQuerySetMixin, APIView):
    permission_classes = [IsAuthenticated]
    authorized_queryset = AuthorizedQuerysetSpec(
        getter=get_authorized_aist_pipelines,
        permission=Permissions.Product_View,
    )

    @extend_schema(
        request=None,
        responses={200: PipelineStopResponseSerializer},
        tags=[AISTApiTag.PIPELINES.value],
    )
    def post(self, request, pipeline_id: str):
        pipeline = self.get_authorized_object(permission=Permissions.Product_Edit, id=pipeline_id)
        user_has_permission_or_403(request.user, pipeline.project.product, Permissions.Product_Edit)
        stop_pipeline(pipeline)
        return Response({"ok": True})


class ExportAIResultsAPI(AuthorizedQuerySetMixin, APIView):
    permission_classes = [IsAuthenticated]
    authorized_queryset = AuthorizedQuerysetSpec(
        getter=get_authorized_aist_pipelines,
        permission=Permissions.Product_View,
    )

    @extend_schema(
        request=ExportAIResultsRequestSerializer,
        responses={
            200: OpenApiResponse(description="Export file"),
            400: OpenApiResponse(description="No AI responses available for export"),
        },
        tags=[AISTApiTag.PIPELINES.value],
    )
    def post(self, request, pipeline_id: str):
        pipeline = self.get_authorized_object(id=pipeline_id)
        serializer = ExportAIResultsRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        return export_ai_results_response(
            pipeline=pipeline,
            params=serializer.validated_data,
        )


class PipelineLogsProgressiveAPI(AuthorizedQuerySetMixin, APIView):
    permission_classes = [IsAuthenticated]
    authorized_queryset = AuthorizedQuerysetSpec(
        getter=get_authorized_aist_pipelines,
        permission=Permissions.Product_View,
    )

    @extend_schema(
        responses={200: OpenApiResponse(description="Log chunk")},
        tags=[AISTApiTag.PIPELINES.value],
    )
    def get(self, request, pipeline_id: str):
        pipeline = self.get_authorized_object(id=pipeline_id)
        serializer = PipelineLogsProgressiveQuerySerializer(data=request.query_params)
        serializer.is_valid(raise_exception=True)
        return pipeline_logs_progressive_response(
            pipeline=pipeline,
            start=serializer.validated_data.get("start"),
            tail=serializer.validated_data.get("tail"),
        )


class PipelineLogsFullAPI(AuthorizedQuerySetMixin, APIView):
    permission_classes = [IsAuthenticated]
    authorized_queryset = AuthorizedQuerysetSpec(
        getter=get_authorized_aist_pipelines,
        permission=Permissions.Product_View,
    )

    @extend_schema(
        responses={200: OpenApiResponse(description="Full log")},
        tags=[AISTApiTag.PIPELINES.value],
    )
    def get(self, request, pipeline_id: str):
        pipeline = self.get_authorized_object(id=pipeline_id)
        return pipeline_logs_full_response(pipeline)


class PipelineLogsDownloadAPI(AuthorizedQuerySetMixin, APIView):
    permission_classes = [IsAuthenticated]
    authorized_queryset = AuthorizedQuerysetSpec(
        getter=get_authorized_aist_pipelines,
        permission=Permissions.Product_View,
    )

    @extend_schema(
        responses={200: OpenApiResponse(description="Log download")},
        tags=[AISTApiTag.PIPELINES.value],
    )
    def get(self, request, pipeline_id: str):
        pipeline = self.get_authorized_object(id=pipeline_id)
        return pipeline_logs_download_response(pipeline)


class PipelineLogsStreamAPI(AuthorizedQuerySetMixin, APIView):
    permission_classes = [IsAuthenticated]
    authorized_queryset = AuthorizedQuerysetSpec(
        getter=get_authorized_aist_pipelines,
        permission=Permissions.Product_View,
    )

    @extend_schema(
        responses={200: OpenApiResponse(description="SSE stream")},
        tags=[AISTApiTag.PIPELINES.value],
    )
    def get(self, request, pipeline_id: str):
        pipeline = self.get_authorized_object(id=pipeline_id)
        return stream_logs_sse_response(pipeline)


class PipelineLogsStreamRedisAPI(AuthorizedQuerySetMixin, APIView):
    permission_classes = [IsAuthenticated]
    authorized_queryset = AuthorizedQuerysetSpec(
        getter=get_authorized_aist_pipelines,
        permission=Permissions.Product_View,
    )

    @extend_schema(
        responses={200: OpenApiResponse(description="SSE stream (redis)")},
        tags=[AISTApiTag.PIPELINES.value],
    )
    def get(self, request, pipeline_id: str):
        pipeline = self.get_authorized_queryset().only("id").filter(id=pipeline_id).first()
        if not pipeline:
            return Response({"detail": "Pipeline not found"}, status=status.HTTP_404_NOT_FOUND)
        return stream_logs_sse_redis_response(pipeline)


class PipelineStatusStreamAPI(AuthorizedQuerySetMixin, APIView):
    permission_classes = [IsAuthenticated]
    authorized_queryset = AuthorizedQuerysetSpec(
        getter=get_authorized_aist_pipelines,
        permission=Permissions.Product_View,
    )

    @extend_schema(
        responses={200: OpenApiResponse(description="Status SSE stream")},
        tags=[AISTApiTag.PIPELINES.value],
    )
    def get(self, request, pipeline_id: str):
        if not self.get_authorized_queryset().filter(id=pipeline_id).exists():
            return Response({"detail": "Pipeline not found"}, status=status.HTTP_404_NOT_FOUND)
        return pipeline_status_stream_response(pipeline_id)


class PipelineDeduplicationProgressAPI(AuthorizedQuerySetMixin, APIView):
    permission_classes = [IsAuthenticated]
    authorized_queryset = AuthorizedQuerysetSpec(
        getter=get_authorized_aist_pipelines,
        permission=Permissions.Product_View,
    )

    @extend_schema(
        responses={200: OpenApiResponse(description="Deduplication progress")},
        tags=[AISTApiTag.PIPELINES.value],
    )
    def get(self, request, pipeline_id: str):
        pipeline = self.get_authorized_object(id=pipeline_id)
        return Response(deduplication_progress_payload(pipeline))


class PipelineEnrichProgressAPI(AuthorizedQuerySetMixin, APIView):
    permission_classes = [IsAuthenticated]
    authorized_queryset = AuthorizedQuerysetSpec(
        getter=get_authorized_aist_pipelines,
        permission=Permissions.Product_View,
    )

    @extend_schema(
        responses={200: OpenApiResponse(description="Enrichment SSE stream")},
        tags=[AISTApiTag.PIPELINES.value],
    )
    def get(self, request, pipeline_id: str):
        if not self.get_authorized_queryset().filter(id=pipeline_id).exists():
            return Response({"detail": "Pipeline not found"}, status=status.HTTP_404_NOT_FOUND)
        return pipeline_enrich_progress_response(pipeline_id)


class PipelineSourceInfoSerializer(serializers.Serializer):
    pipeline_id = serializers.CharField()
    status = serializers.CharField()
    project_path = serializers.CharField()
    project_name = serializers.CharField()
    languages = serializers.ListField(child=serializers.CharField())


class PipelineSourceInfoAPI(APIView):

    """Internal endpoint for MCP services to resolve pipeline source path."""

    permission_classes = [IsAuthenticated]

    @extend_schema(
        responses={
            200: PipelineSourceInfoSerializer,
            404: OpenApiResponse(description="Pipeline not found"),
            409: OpenApiResponse(description="Sources not available"),
        },
        tags=[AISTApiTag.PIPELINES.value],
        summary="Get pipeline source info (internal)",
        description=(
            "Returns the on-disk source path for a running pipeline. "
            "Used by context-extractor and filesystem MCP servers."
        ),
    )
    def get(self, request, pipeline_id: str):
        pipeline = get_object_or_404(
            AISTPipeline.objects.select_related("project__product"),
            id=pipeline_id,
        )

        if is_terminal_pipeline_status(pipeline.status):
            return Response(
                {"detail": "Pipeline is in terminal status, sources no longer available"},
                status=status.HTTP_409_CONFLICT,
            )

        ld = PipelineLaunchData(pipeline.launch_data)
        if not ld.project_path:
            return Response(
                {"detail": "Source path not yet available"},
                status=status.HTTP_409_CONFLICT,
            )

        product_name = getattr(pipeline.project.product, "name", "")
        source_root = ld.resolve_source_root(product_name)

        return Response({
            "pipeline_id": pipeline.id,
            "status": pipeline.status,
            "project_path": source_root,
            "project_name": product_name,
            "languages": ld.languages or [],
        })
