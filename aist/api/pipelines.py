from __future__ import annotations

import csv
import json
import re
import time
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime  # noqa: TC003 -- drf-spectacular resolves serializer type hints at runtime
from io import BytesIO, StringIO
from operator import itemgetter
from typing import TYPE_CHECKING

from django.db import close_old_connections
from django.db.models import Count
from django.http import HttpResponse, HttpResponseBadRequest, StreamingHttpResponse
from django_filters import rest_framework as django_filters
from drf_spectacular.utils import OpenApiExample, OpenApiParameter, OpenApiResponse, extend_schema
from openpyxl import Workbook
from rest_framework import generics, serializers, status
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response
from rest_framework.settings import api_settings

from aist.ai_filter import validate_and_normalize_filter
from aist.api.bootstrap import _import_sast_pipeline_package  # noqa: F401
from aist.api.launch_requests import (
    LaunchRequestResponseSerializer,
    launch_principal_token,
    launch_request_headers,
    launch_request_response,
)
from aist.api.schema import AISTApiTag
from aist.authz import INTERNAL_SERVICE, Action, AISTAPIView, AISTAuthzMixin, ResourcePolicy, queryset_for_action
from aist.execution.enqueue import LaunchEnqueueError, LaunchPrincipal, enqueue_pipeline_launch
from aist.launch_data import PipelineLaunchData
from aist.logging_transport import (
    BACKLOG_COUNT,
    LOG_ROTATION_BACKUP_COUNT,
    PUBSUB_CHANNEL_TPL,
    STREAM_KEY,
    get_pipeline_bridge_log_path,
    get_pipeline_log_path,
    get_redis,
)
from aist.models import AISTPipeline, AISTProjectVersion, AISTStatus, DastExecutionState, TestDeduplicationProgress
from aist.pipeline_args import PipelineArguments
from aist.services.dast_outcomes import public_dast_outcome_code
from aist.services.dast_run_metadata import dast_run_detail
from aist.utils.export import _build_ai_export_rows
from aist.utils.pipeline import (
    is_terminal_pipeline_status,
    stop_pipeline,
)

if TYPE_CHECKING:
    from pathlib import Path


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

    def to_internal_value(self, data):
        unknown = set(data) - set(self.fields)
        if unknown:
            raise serializers.ValidationError(dict.fromkeys(sorted(unknown), "Unknown field."))
        return super().to_internal_value(data)

    def get_fields(self):
        fields = super().get_fields()
        request = self.context.get("request")
        if request and getattr(request, "user", None) and request.user.is_authenticated:
            fields["project_version_id"].queryset = queryset_for_action(
                resource=AISTProjectVersion,
                action=Action.PROJECT_OPERATE,
                user=request.user,
            )
        return fields


class PipelineResponseSerializer(serializers.Serializer):
    id = serializers.CharField()
    execution_type = serializers.CharField()
    status = serializers.CharField()
    run_task_id = serializers.CharField(allow_null=True)
    external_run_id = serializers.SerializerMethodField()
    external_log_cursor = serializers.SerializerMethodField()
    external_execution_outcome = serializers.SerializerMethodField()
    dast_outcome_code = serializers.SerializerMethodField()
    external_cancel_requested_at = serializers.SerializerMethodField()
    response_from_ai = serializers.JSONField(allow_null=True)
    created = serializers.DateTimeField()
    updated = serializers.DateTimeField()

    def get_dast_outcome_code(self, obj: AISTPipeline) -> str | None:
        return public_dast_outcome_code(obj)

    @staticmethod
    def _dast_state(obj: AISTPipeline):
        try:
            return obj.dast_execution_state
        except DastExecutionState.DoesNotExist:
            return None

    def get_external_run_id(self, obj: AISTPipeline) -> str | None:
        state = self._dast_state(obj)
        return state.run_id if state is not None else None

    def get_external_log_cursor(self, obj: AISTPipeline) -> int:
        state = self._dast_state(obj)
        return state.log_cursor if state is not None else 0

    def get_external_execution_outcome(self, obj: AISTPipeline) -> str:
        state = self._dast_state(obj)
        return state.outcome if state is not None else ""

    def get_external_cancel_requested_at(self, obj: AISTPipeline) -> datetime | None:
        state = self._dast_state(obj)
        return state.cancel_requested_at if state is not None else None


class PipelineStopResponseSerializer(serializers.Serializer):
    ok = serializers.BooleanField()
    state = serializers.CharField()


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
    # Independent offset into <pipeline_id>.bridge.log; the response carries
    # both X-Log-Size (main) and X-Bridge-Log-Size headers so the client can
    # advance each cursor without losing byte stability per source.
    bridge_start = serializers.IntegerField(required=False, min_value=0)


class PipelineStartAPI(AISTAPIView):

    """Queue a new AIST pipeline launch request."""

    authz = ResourcePolicy(resource=AISTProjectVersion, read=Action.PRODUCT_READ, write=Action.PROJECT_OPERATE)

    @extend_schema(
        request=PipelineStartRequestSerializer,
        parameters=[
            OpenApiParameter(
                name="Idempotency-Key",
                type=str,
                location=OpenApiParameter.HEADER,
                required=False,
            ),
        ],
        responses={
            202: OpenApiResponse(LaunchRequestResponseSerializer, description="Launch request queued"),
            404: OpenApiResponse(description="Project version not found"),
        },
        examples=[
            OpenApiExample(
                "Start by version id",
                value={
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
        summary="Queue pipeline launch",
        description="Queues a durable SAST launch request for an existing AISTProjectVersion.",
    )
    def post(self, request, *args, **kwargs) -> Response:
        if api_settings.URL_FORMAT_OVERRIDE:
            setattr(request, api_settings.URL_FORMAT_OVERRIDE, None)

        # validate body
        serializer = PipelineStartRequestSerializer(data=request.data, context={"request": request})
        serializer.is_valid(raise_exception=True)
        project_version = serializer.validated_data["project_version_id"]
        project = project_version.project
        provided_ai_filter = serializer.validated_data.get("ai_filter", None)

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

        organization = project.organization
        if organization is None:
            return Response(
                {"organization": "Project does not belong to an AIST organization."},
                status=status.HTTP_409_CONFLICT,
            )
        principal = LaunchPrincipal.for_user(
            organization=organization,
            requester=request.user,
            api_token=launch_principal_token(request),
        )
        try:
            launch_request = enqueue_pipeline_launch(
                arguments=PipelineArguments.for_sast(project=project, raw_params=raw),
                principal=principal,
                client_request_key=launch_request_headers(request).get("client_request_key"),
            ).request
        except LaunchEnqueueError as exc:
            raise ValidationError({"detail": str(exc)}) from exc
        return Response(launch_request_response(launch_request), status=status.HTTP_202_ACCEPTED)


class PipelineListAPI(AISTAuthzMixin, generics.ListAPIView):

    """Paginated list of pipelines with simple filtering."""

    serializer_class = PipelineResponseSerializer
    authz = ResourcePolicy(resource=AISTPipeline, read=Action.PRODUCT_READ, write=Action.PROJECT_OPERATE)

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
                self.authorized_queryset()
                .select_related("project", "project_version", "dast_execution_state")
                .order_by("-created")
            ),
            request=self.request,
        )
        if not filterset.is_valid():
            raise ValidationError(filterset.errors)
        return filterset.qs


class PipelineAPI(AISTAPIView):

    """Retrieve or delete a pipeline by id."""

    authz = ResourcePolicy(resource=AISTPipeline, read=Action.PRODUCT_READ, write=Action.PROJECT_OPERATE)

    @extend_schema(
        responses={200: PipelineResponseSerializer, 404: OpenApiResponse(description="Not found")},
        tags=[AISTApiTag.PIPELINES.value],
        summary="Get pipeline status",
        description="Returns pipeline status and AI response.",
    )
    def get(self, request, pipeline_id: str, *args, **kwargs) -> Response:
        pipeline = self.resolve(id=pipeline_id)
        out = PipelineResponseSerializer(pipeline)
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
        p = self.resolve(id=pipeline_id)
        if not is_terminal_pipeline_status(p.status):
            return Response(status=status.HTTP_400_BAD_REQUEST)
        p.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class PipelineDastRunAPI(AISTAPIView):

    """Provider-reported run metadata of the DAST report accepted onto this pipeline."""

    authz = ResourcePolicy(resource=AISTPipeline, read=Action.PRODUCT_READ, write=Action.PRODUCT_READ)
    token_read_only = True

    @extend_schema(
        responses={
            200: OpenApiResponse(description="An envelope whose dast_run field is the metadata, or null"),
            404: OpenApiResponse(description="Not found"),
        },
        tags=[AISTApiTag.PIPELINES.value],
        summary="Get DAST run metadata",
        description=(
            "Coverage and agent token usage as reported by the accepted DAST report, for both "
            "autonomous runs and operator uploads. Every field is optional: a key is absent "
            "whenever the report did not carry it, and `dast_run` itself is null for a pipeline "
            "with no accepted DAST report. Wrapped in an envelope so absence is still a valid "
            "JSON body rather than an empty response."
        ),
    )
    def get(self, request, pipeline_id: str, *args, **kwargs) -> Response:
        pipeline = self.resolve(id=pipeline_id)
        return Response({"dast_run": dast_run_detail(pipeline)}, status=status.HTTP_200_OK)


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


# Leading timestamp emitted by ``RotatingFileHandler`` formatter
# ``%(asctime)s [%(levelname)s] %(message)s`` — lexicographically sortable.
_LOG_TS_RE = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}(?:[,.]\d+)?)")


def _iter_rotated_log_paths(base: Path) -> list[Path]:
    """
    Return existing log files for ``base`` in chronological order: the oldest
    surviving backup first, the live file last.

    ``RotatingFileHandler`` writes the most recent rotation to ``base.1`` and
    pushes older content towards ``base.N`` (the highest-numbered backup is
    the oldest), so the chronological sequence is
    ``base.N → base.N-1 → … → base.1 → base``. Missing numbered backups are
    skipped (rotation may not have happened yet). Only numbered suffixes up
    to ``LOG_ROTATION_BACKUP_COUNT`` are considered — a glob over ``*.log.*``
    would pull in unrelated files that happen to share the prefix.
    """
    ordered: list[Path] = []
    for index in range(LOG_ROTATION_BACKUP_COUNT, 0, -1):
        candidate = base.with_name(f"{base.name}.{index}")
        if candidate.exists():
            ordered.append(candidate)
    if base.exists():
        ordered.append(base)
    return ordered


def _read_file_text(path: Path) -> str:
    """
    Return the concatenated text of ``path`` and any surviving rotated
    backups in chronological order. Missing files → empty string. The
    single-file fast path is byte-identical to the pre-rotation behavior.
    """
    parts: list[str] = []
    for candidate in _iter_rotated_log_paths(path):
        try:
            parts.append(candidate.read_text(encoding="utf-8", errors="ignore"))
        except OSError:
            continue
    return "".join(parts)


def _merged_pipeline_log_text(pipeline_id: str) -> str:
    """Full timestamp-merged view of celeryworker + aist-triage-bridge files."""
    return _merge_log_chunks(
        _read_file_text(get_pipeline_log_path(pipeline_id)),
        _read_file_text(get_pipeline_bridge_log_path(pipeline_id)),
    )


def _read_file_chunk(path: Path, *, start: int | None, tail: int | None) -> tuple[str, int]:
    """
    Return ``(text, file_size)`` from ``path`` honoring ``start`` / ``tail``.

    ``start`` reads bytes from that offset to EOF; ``tail`` returns the last N
    lines. Missing file → empty text + size 0.
    """
    if not path.exists():
        return "", 0
    size = path.stat().st_size
    if tail:
        with path.open("rb") as f:
            lines = f.readlines()[-tail:]
        text = "\n".join(ln.decode("utf-8", errors="ignore").rstrip("\r\n") for ln in lines)
    elif start is not None:
        clamped = max(0, min(start, size))
        with path.open("rb") as f:
            f.seek(clamped)
            chunk = f.read()
        text = chunk.decode("utf-8", errors="ignore")
    else:
        text = path.read_text(encoding="utf-8", errors="ignore")
    return text, size


def _merge_log_chunks(main_text: str, bridge_text: str) -> str:
    """
    Merge a main-log chunk and a bridge-log chunk by leading timestamp.

    Each side already arrives in chronological order. We tag bridge lines
    with a ``[bridge]`` prefix so the operator can distinguish them, then
    interleave by parsed timestamp. Lines without a timestamp (continuation
    lines from multi-line messages) inherit the previous line's timestamp
    so they stay glued to their header.
    """
    if not bridge_text:
        return main_text
    if not main_text:
        return _annotate_bridge_lines(bridge_text)

    main_records = _records_with_timestamp(main_text, source_tag="")
    bridge_records = _records_with_timestamp(bridge_text, source_tag="[bridge] ")
    # Stable merge: equal timestamps preserve "main before bridge".
    indexed = [(ts, 0, txt) for ts, txt in main_records] + [(ts, 1, txt) for ts, txt in bridge_records]
    indexed.sort(key=itemgetter(0, 1))
    return "\n".join(txt for _, _, txt in indexed)


def _records_with_timestamp(text: str, *, source_tag: str) -> list[tuple[str, str]]:
    """Split ``text`` into ``(ts, line_block)`` records with continuation folding."""
    records: list[list[str]] = []
    current: list[str] | None = None
    for line in text.splitlines():
        match = _LOG_TS_RE.match(line)
        if match:
            current = [match.group(1), source_tag + line]
            records.append(current)
        elif current is not None:
            current.append(source_tag + line)
        else:
            current = ["", source_tag + line]
            records.append(current)
    return [(rec[0], "\n".join(rec[1:])) for rec in records]


def _annotate_bridge_lines(bridge_text: str) -> str:
    """Prefix every line with ``[bridge] `` so operators can tell sources apart."""
    return "\n".join(f"[bridge] {line}" for line in bridge_text.splitlines())


def pipeline_logs_progressive_response(
    *,
    pipeline: AISTPipeline,
    start: int | None,
    tail: int | None,
    bridge_start: int | None = None,
) -> HttpResponse:
    """
    Progressive log read merging celeryworker + aist-triage-bridge files.

    Each source is byte-stable on its own (append-only). The client tracks
    two independent offsets (``start`` for the main file, ``bridge_start``
    for the bridge file) and the response carries two size headers so the
    next poll can resume from the correct byte position in EACH file.

    Body is the timestamp-merged delta of both files since the supplied
    offsets, with bridge lines prefixed ``[bridge] `` so operators can
    distinguish sources at a glance. Initial requests use ``tail=N`` to
    fetch the last N merged lines.
    """
    main_path = get_pipeline_log_path(pipeline.id)
    bridge_path = get_pipeline_bridge_log_path(pipeline.id)

    if tail:
        main_full, main_size = _read_file_chunk(main_path, start=None, tail=None)
        bridge_full, bridge_size = _read_file_chunk(bridge_path, start=None, tail=None)
        merged = _merge_log_chunks(main_full, bridge_full)
        merged_lines = merged.splitlines()
        body = "\n".join(merged_lines[-tail:]) if tail else merged
    else:
        main_chunk, main_size = _read_file_chunk(main_path, start=start, tail=None)
        bridge_chunk, bridge_size = _read_file_chunk(bridge_path, start=bridge_start, tail=None)
        body = _merge_log_chunks(main_chunk, bridge_chunk)

    resp = HttpResponse(body, content_type="text/plain; charset=utf-8")
    resp["X-Log-Size"] = str(main_size)
    resp["X-Bridge-Log-Size"] = str(bridge_size)
    return resp


def pipeline_logs_full_response(pipeline: AISTPipeline) -> HttpResponse:
    return HttpResponse(
        _merged_pipeline_log_text(pipeline.id),
        content_type="text/plain; charset=utf-8",
    )


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

    def _iter_backlog_lines(entries):
        for _entry_id, fields in reversed(entries):  # B007
            pid = (fields or {}).get("pipeline_id")
            msg = (fields or {}).get("message")
            lvl = (fields or {}).get("level")
            if not pid or pid != pipeline.id or not msg:
                continue
            line = f"{lvl} {msg}" if lvl else msg
            yield _sse_data(line)

    def _stream_last_lines_from_redis_stream(limit: int):
        try:
            entries = r.xrevrange(STREAM_KEY, max="+", min="-", count=limit) or []
            yield from _iter_backlog_lines(entries)
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


class PipelineStopAPI(AISTAPIView):
    authz = ResourcePolicy(resource=AISTPipeline, read=Action.PRODUCT_READ, write=Action.PROJECT_OPERATE)

    @extend_schema(
        request=None,
        responses={200: PipelineStopResponseSerializer},
        tags=[AISTApiTag.PIPELINES.value],
    )
    def post(self, request, pipeline_id: str):
        pipeline = self.resolve(id=pipeline_id)
        stop_pipeline(pipeline)
        pipeline.refresh_from_db(fields=["status"])
        execution_state = getattr(pipeline, "dast_execution_state", None)
        return Response({
            "ok": True,
            "state": (execution_state.outcome if execution_state is not None else "") or pipeline.status,
        })


class ExportAIResultsAPI(AISTAPIView):
    authz = ResourcePolicy(resource=AISTPipeline, read=Action.PRODUCT_READ, write=Action.PRODUCT_READ)
    token_read_only = True

    @extend_schema(
        request=ExportAIResultsRequestSerializer,
        responses={
            200: OpenApiResponse(description="Export file"),
            400: OpenApiResponse(description="No AI responses available for export"),
        },
        tags=[AISTApiTag.PIPELINES.value],
    )
    def post(self, request, pipeline_id: str):
        pipeline = self.resolve(id=pipeline_id)
        serializer = ExportAIResultsRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        return export_ai_results_response(
            pipeline=pipeline,
            params=serializer.validated_data,
        )


class PipelineLogsProgressiveAPI(AISTAPIView):
    authz = ResourcePolicy(resource=AISTPipeline, read=Action.PRODUCT_READ, write=Action.PROJECT_OPERATE)

    @extend_schema(
        responses={200: OpenApiResponse(description="Log chunk")},
        tags=[AISTApiTag.PIPELINES.value],
    )
    def get(self, request, pipeline_id: str):
        pipeline = self.resolve(id=pipeline_id)
        serializer = PipelineLogsProgressiveQuerySerializer(data=request.query_params)
        serializer.is_valid(raise_exception=True)
        return pipeline_logs_progressive_response(
            pipeline=pipeline,
            start=serializer.validated_data.get("start"),
            tail=serializer.validated_data.get("tail"),
            bridge_start=serializer.validated_data.get("bridge_start"),
        )


class PipelineLogsFullAPI(AISTAPIView):
    authz = ResourcePolicy(resource=AISTPipeline, read=Action.PRODUCT_READ, write=Action.PROJECT_OPERATE)

    @extend_schema(
        responses={200: OpenApiResponse(description="Full log")},
        tags=[AISTApiTag.PIPELINES.value],
    )
    def get(self, request, pipeline_id: str):
        pipeline = self.resolve(id=pipeline_id)
        return pipeline_logs_full_response(pipeline)


class PipelineLogsDownloadAPI(AISTAPIView):
    authz = ResourcePolicy(resource=AISTPipeline, read=Action.PRODUCT_READ, write=Action.PROJECT_OPERATE)

    @extend_schema(
        responses={200: OpenApiResponse(description="Log download")},
        tags=[AISTApiTag.PIPELINES.value],
    )
    def get(self, request, pipeline_id: str):
        pipeline = self.resolve(id=pipeline_id)
        return pipeline_logs_download_response(pipeline)


class PipelineLogsStreamAPI(AISTAPIView):
    authz = ResourcePolicy(resource=AISTPipeline, read=Action.PRODUCT_READ, write=Action.PROJECT_OPERATE)

    @extend_schema(
        responses={200: OpenApiResponse(description="SSE stream")},
        tags=[AISTApiTag.PIPELINES.value],
    )
    def get(self, request, pipeline_id: str):
        pipeline = self.resolve(id=pipeline_id)
        return stream_logs_sse_response(pipeline)


class PipelineLogsStreamRedisAPI(AISTAPIView):
    authz = ResourcePolicy(resource=AISTPipeline, read=Action.PRODUCT_READ, write=Action.PROJECT_OPERATE)

    @extend_schema(
        responses={200: OpenApiResponse(description="SSE stream (redis)")},
        tags=[AISTApiTag.PIPELINES.value],
    )
    def get(self, request, pipeline_id: str):
        pipeline = self.authorized_queryset().only("id").filter(id=pipeline_id).first()
        if not pipeline:
            return Response({"detail": "Pipeline not found"}, status=status.HTTP_404_NOT_FOUND)
        return stream_logs_sse_redis_response(pipeline)


class PipelineStatusStreamAPI(AISTAPIView):
    authz = ResourcePolicy(resource=AISTPipeline, read=Action.PRODUCT_READ, write=Action.PROJECT_OPERATE)

    @extend_schema(
        responses={200: OpenApiResponse(description="Status SSE stream")},
        tags=[AISTApiTag.PIPELINES.value],
    )
    def get(self, request, pipeline_id: str):
        if not self.authorized_queryset().filter(id=pipeline_id).exists():
            return Response({"detail": "Pipeline not found"}, status=status.HTTP_404_NOT_FOUND)
        return pipeline_status_stream_response(pipeline_id)


class PipelineDeduplicationProgressAPI(AISTAPIView):
    authz = ResourcePolicy(resource=AISTPipeline, read=Action.PRODUCT_READ, write=Action.PROJECT_OPERATE)

    @extend_schema(
        responses={200: OpenApiResponse(description="Deduplication progress")},
        tags=[AISTApiTag.PIPELINES.value],
    )
    def get(self, request, pipeline_id: str):
        pipeline = self.resolve(id=pipeline_id)
        return Response(deduplication_progress_payload(pipeline))


class PipelineEnrichProgressAPI(AISTAPIView):
    authz = ResourcePolicy(resource=AISTPipeline, read=Action.PRODUCT_READ, write=Action.PROJECT_OPERATE)

    @extend_schema(
        responses={200: OpenApiResponse(description="Enrichment SSE stream")},
        tags=[AISTApiTag.PIPELINES.value],
    )
    def get(self, request, pipeline_id: str):
        if not self.authorized_queryset().filter(id=pipeline_id).exists():
            return Response({"detail": "Pipeline not found"}, status=status.HTTP_404_NOT_FOUND)
        return pipeline_enrich_progress_response(pipeline_id)


class PipelineSourceInfoSerializer(serializers.Serializer):
    pipeline_id = serializers.CharField()
    status = serializers.CharField()
    project_path = serializers.CharField()
    project_name = serializers.CharField()
    languages = serializers.ListField(child=serializers.CharField())


class PipelineSourceInfoAPI(AISTAPIView):

    """Internal endpoint for MCP services to resolve pipeline source path."""

    authz = INTERNAL_SERVICE

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
        pipeline = (
            queryset_for_action(resource=AISTPipeline, action=Action.PRODUCT_READ, user=request.user)
            .select_related("project__product")
            .filter(id=pipeline_id)
            .first()
        )
        if pipeline is None:
            return Response({"detail": "Pipeline not found"}, status=status.HTTP_404_NOT_FOUND)

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
