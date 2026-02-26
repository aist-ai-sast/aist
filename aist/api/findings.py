from __future__ import annotations

import csv
import re
from dataclasses import dataclass
from io import BytesIO, StringIO
from urllib.parse import urlsplit

from django.http import HttpResponse
from django.shortcuts import get_object_or_404
from django_filters import rest_framework as django_filters
from dojo.api_v2 import serializers as dojo_serializers
from dojo.authorization.roles_permissions import Permissions
from dojo.filters import ApiFindingFilter
from dojo.models import Notes
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, OpenApiResponse, extend_schema, extend_schema_field
from openpyxl import Workbook
from rest_framework import serializers, status
from rest_framework.pagination import LimitOffsetPagination
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from aist.api.common import API_SEVERITY_VALUES
from aist.api.query import AuthorizedQuerySetMixin, AuthorizedQuerysetSpec
from aist.models import AISTAIFindingResponse, VersionType
from aist.queries import get_authorized_aist_pipelines, get_authorized_findings


@dataclass(frozen=True, slots=True)
class FindingApiChoices:
    ai_status: list[str]
    severity: list[str]
    export_format: list[str]
    ordering: list[str]


FINDING_API_CHOICES = FindingApiChoices(
    ai_status=["has_ai", "no_ai", "ai_tp", "ai_fp", "ai_u"],
    severity=list(API_SEVERITY_VALUES),
    export_format=["csv", "xlsx"],
    ordering=list(getattr(ApiFindingFilter.base_filters.get("o"), "param_map", {}).keys()),
)


def _normalize_external_reference(raw: str) -> str:
    value = (raw or "").strip()
    if not value:
        return ""
    parsed = urlsplit(value)
    if parsed.scheme:
        return value
    if " " in value:
        return value
    if "." not in value:
        return value
    return f"https://{value}"


def _extract_code_snippet(description: str | None) -> str:
    text = (description or "").strip()
    if not text:
        return ""
    match = re.search(r"```(?:[\w.+-]+)?\n(?P<snippet>.*?)```", text, flags=re.DOTALL)
    if not match:
        return ""
    return match.group("snippet").strip()


def _pick_project_version_info(finding) -> tuple[str | None, str | None, int | None]:
    versions_rel = getattr(finding, "aist_project_versions", None)
    if versions_rel is None:
        return None, None, None
    versions = list(versions_rel.all())
    if not versions:
        return None, None, None
    hash_version = next((v for v in versions if v.version_type == VersionType.GIT_HASH), None)
    if hash_version:
        return hash_version.version, hash_version.version_type, hash_version.project_id
    branch_version = next((v for v in versions if v.version_type == VersionType.GIT_BRANCH), None)
    if branch_version:
        return branch_version.version, branch_version.version_type, branch_version.project_id
    first = versions[0]
    return first.version, first.version_type, first.project_id


class AISTFindingListItemSerializer(dojo_serializers.FindingSerializer):
    project_version = serializers.SerializerMethodField()
    project_version_type = serializers.SerializerMethodField()
    project_id = serializers.SerializerMethodField()
    created = serializers.SerializerMethodField()

    @extend_schema_field(OpenApiTypes.STR)
    def get_project_version(self, obj) -> str | None:
        version, _version_type, _project_id = _pick_project_version_info(obj)
        return version

    @extend_schema_field(OpenApiTypes.STR)
    def get_project_version_type(self, obj) -> str | None:
        _version, version_type, _project_id = _pick_project_version_info(obj)
        return version_type

    @extend_schema_field(OpenApiTypes.INT)
    def get_project_id(self, obj) -> int | None:
        _version, _version_type, project_id = _pick_project_version_info(obj)
        return project_id

    @extend_schema_field(OpenApiTypes.DATETIME)
    def get_created(self, obj) -> str | None:
        created = getattr(obj, "date", None) or getattr(obj, "created", None)
        return created.isoformat() if created else None


class AISTFindingFilter(ApiFindingFilter):
    pipeline_id = django_filters.CharFilter(method="filter_pipeline_id")
    project_id = django_filters.NumberFilter(field_name="aist_project_versions__project_id")
    created_gte = django_filters.IsoDateTimeFilter(field_name="date", lookup_expr="gte")
    created_lte = django_filters.IsoDateTimeFilter(field_name="date", lookup_expr="lte")
    project_version = django_filters.CharFilter(field_name="aist_project_versions__version", lookup_expr="exact")
    file = django_filters.CharFilter(field_name="file_path", lookup_expr="icontains")
    ai_status = django_filters.ChoiceFilter(
        method="filter_ai_status",
        choices=[(value, value) for value in FINDING_API_CHOICES.ai_status],
    )
    ordering = django_filters.OrderingFilter(
        fields=tuple(
            (field_name, param_name)
            for param_name, field_name in getattr(ApiFindingFilter.base_filters.get("o"), "param_map", {}).items()
        ),
    )

    def filter_pipeline_id(self, queryset, name, value):
        pipeline_id = (value or "").strip()
        if not pipeline_id:
            return queryset
        pipeline = (
            get_authorized_aist_pipelines(Permissions.Product_View, user=self.request.user)
            .filter(id=pipeline_id)
            .first()
        )
        if not pipeline:
            return queryset.none()
        return queryset.filter(test__aist_pipelines=pipeline)

    def filter_ai_status(self, queryset, name, value):
        status_value = (value or "").strip().lower()
        if not status_value:
            return queryset

        ai_qs = AISTAIFindingResponse.objects.all()
        pipeline_id = (self.data.get("pipeline_id") or "").strip()
        if pipeline_id:
            ai_qs = ai_qs.filter(pipeline_id=pipeline_id)
        if status_value == "ai_tp":
            ai_qs = ai_qs.filter(verdict=AISTAIFindingResponse.Verdict.TRUE_POSITIVE)
        elif status_value == "ai_fp":
            ai_qs = ai_qs.filter(verdict=AISTAIFindingResponse.Verdict.FALSE_POSITIVE)
        elif status_value == "ai_u":
            ai_qs = ai_qs.filter(verdict=AISTAIFindingResponse.Verdict.UNCERTAIN)

        ai_finding_ids = ai_qs.values_list("finding_id", flat=True)
        if status_value in {"has_ai", "ai_tp", "ai_fp", "ai_u"}:
            return queryset.filter(id__in=ai_finding_ids)
        return queryset.exclude(id__in=ai_finding_ids)


class AISTFindingListAPI(AuthorizedQuerySetMixin, APIView):
    permission_classes = [IsAuthenticated]
    authorized_queryset = AuthorizedQuerysetSpec(
        getter=get_authorized_findings,
        permission=Permissions.Finding_View,
    )

    @extend_schema(
        tags=["aist"],
        summary="List AIST findings",
        parameters=[
            OpenApiParameter(name="pipeline_id", required=False, type=str),
            OpenApiParameter(name="tags", required=False, type=str, many=True),
            OpenApiParameter(name="severity", required=False, type=str, many=True, enum=FINDING_API_CHOICES.severity),
            OpenApiParameter(
                name="ai_status",
                required=False,
                type=str,
                enum=FINDING_API_CHOICES.ai_status,
            ),
            OpenApiParameter(name="project_id", required=False, type=int),
            OpenApiParameter(name="created_gte", required=False, type=str),
            OpenApiParameter(name="created_lte", required=False, type=str),
            OpenApiParameter(name="project_version", required=False, type=str),
            OpenApiParameter(name="file", required=False, type=str),
            OpenApiParameter(name="ordering", required=False, type=str, enum=FINDING_API_CHOICES.ordering),
            OpenApiParameter(name="limit", required=False, type=int),
            OpenApiParameter(name="offset", required=False, type=int),
        ],
        responses={200: AISTFindingListItemSerializer(many=True)},
    )
    def get(self, request, *args, **kwargs) -> Response:
        queryset = (
            self.get_authorized_queryset()
            .select_related("test__engagement")
            .prefetch_related("tags", "aist_project_versions")
        )
        filterset = AISTFindingFilter(data=request.query_params, queryset=queryset, request=request)
        if not filterset.is_valid():
            return Response(filterset.errors, status=status.HTTP_400_BAD_REQUEST)
        queryset = filterset.qs.distinct()

        paginator = LimitOffsetPagination()
        page = paginator.paginate_queryset(queryset, request)
        serializer = AISTFindingListItemSerializer(page, many=True, context={"request": request})
        return paginator.get_paginated_response(serializer.data)


class AISTFindingNoteSerializer(serializers.ModelSerializer):
    user_display = serializers.SerializerMethodField()

    class Meta:
        model = Notes
        fields = ("id", "entry", "date", "user_display", "private")
        read_only_fields = ("id", "date", "user_display", "private")

    def get_user_display(self, obj) -> str:
        author = getattr(obj, "author", None)
        return (author.username or "").strip() if author else ""


class AISTFindingCreateNoteSerializer(serializers.Serializer):
    entry = serializers.CharField()

    def validate_entry(self, value: str) -> str:
        normalized = (value or "").strip()
        if not normalized:
            msg = "entry is required"
            raise serializers.ValidationError(msg)
        return normalized


class AISTFindingNotesAPI(AuthorizedQuerySetMixin, APIView):
    permission_classes = [IsAuthenticated]
    serializer_class = AISTFindingNoteSerializer
    authorized_queryset = AuthorizedQuerysetSpec(
        getter=get_authorized_findings,
        permission=Permissions.Finding_View,
    )

    @extend_schema(
        tags=["aist"],
        summary="List finding notes",
        responses={200: AISTFindingNoteSerializer(many=True)},
    )
    def get(self, request, finding_id: int):
        finding = self.get_authorized_object(id=finding_id)
        notes = finding.notes.select_related("author").all().order_by("-date")
        out = AISTFindingNoteSerializer(notes, many=True)
        return Response(out.data, status=status.HTTP_200_OK)

    @extend_schema(
        tags=["aist"],
        summary="Add finding note",
        request=AISTFindingCreateNoteSerializer,
        responses={201: AISTFindingNoteSerializer},
    )
    def post(self, request, finding_id: int):
        finding = self.get_authorized_object(permission=Permissions.Finding_Edit, id=finding_id)
        input_serializer = AISTFindingCreateNoteSerializer(data=request.data)
        input_serializer.is_valid(raise_exception=True)

        note = Notes.objects.create(
            entry=input_serializer.validated_data["entry"],
            author=request.user,
            private=False,
        )
        finding.notes.add(note)
        finding.last_reviewed = note.date
        finding.last_reviewed_by = request.user
        finding.save(update_fields=["last_reviewed", "last_reviewed_by", "updated"])
        out = AISTFindingNoteSerializer(note)
        return Response(out.data, status=status.HTTP_201_CREATED)


class AISTFindingExportRequestSerializer(serializers.Serializer):
    format = serializers.ChoiceField(choices=FINDING_API_CHOICES.export_format, required=False, default="csv")


class AISTFindingExportAPI(AuthorizedQuerySetMixin, APIView):
    permission_classes = [IsAuthenticated]
    serializer_class = AISTFindingExportRequestSerializer
    authorized_queryset = AuthorizedQuerysetSpec(
        getter=get_authorized_findings,
        permission=Permissions.Finding_View,
    )

    @extend_schema(
        tags=["aist"],
        summary="Export single finding",
        parameters=[OpenApiParameter(name="format", required=False, type=str, enum=FINDING_API_CHOICES.export_format)],
        request=AISTFindingExportRequestSerializer,
        responses={
            200: OpenApiResponse(description="Export file"),
            400: OpenApiResponse(description="Validation error"),
        },
    )
    def post(self, request, finding_id: int):
        body_serializer = self.serializer_class(data=request.data)
        body_serializer.is_valid(raise_exception=True)
        query_serializer = self.serializer_class(data=request.query_params)
        query_serializer.is_valid(raise_exception=True)

        body_format = body_serializer.validated_data.get("format")
        query_format = query_serializer.validated_data.get("format")
        fmt = body_format or query_format or "csv"

        finding = get_object_or_404(
            self.get_authorized_queryset()
            .select_related("test__engagement__product")
            .prefetch_related("tags", "aist_project_versions"),
            id=finding_id,
        )
        project_version, project_version_type, _project_id = _pick_project_version_info(finding)
        created = getattr(finding, "date", None) or getattr(finding, "created", None)

        pipeline_qs = self.get_authorized_queryset(
            getter=get_authorized_aist_pipelines,
            permission=Permissions.Product_View,
        )
        ai = (
            AISTAIFindingResponse.objects.filter(finding_id=finding.id, pipeline__in=pipeline_qs)
            .select_related("pipeline")
            .order_by("-pipeline__created", "-updated", "-id")
            .first()
        )
        ai_references = ""
        if ai and ai.references:
            ai_references = ", ".join(
                [ref for ref in (_normalize_external_reference(str(item)) for item in ai.references) if ref],
            )
        ai_status = ""
        if ai:
            verdict_map = {
                AISTAIFindingResponse.Verdict.TRUE_POSITIVE: "AI TP",
                AISTAIFindingResponse.Verdict.FALSE_POSITIVE: "AI FP",
                AISTAIFindingResponse.Verdict.UNCERTAIN: "AI U",
            }
            ai_status = verdict_map.get(ai.verdict, ai.verdict or "")

        product = getattr(getattr(getattr(finding, "test", None), "engagement", None), "product", None)
        row = {
            "finding_id": finding.id,
            "title": finding.title or "",
            "severity": finding.severity or "",
            "status": "Active" if finding.active else "Non-Active",
            "project": product.name if product else "",
            "project_version": project_version or "",
            "project_version_type": project_version_type or "",
            "file": finding.file_path or "",
            "line": finding.line or "",
            "cwe": finding.cwe or "",
            "tags": ", ".join([tag.name for tag in finding.tags.all()]),
            "description": finding.description or "",
            "codeSnippet": _extract_code_snippet(finding.description),
            "created": created.isoformat() if created else "",
            "is_mitigated": bool(finding.is_mitigated),
            "risk_accepted": bool(finding.risk_accepted),
            "false_positive": bool(finding.false_p),
            "out_of_scope": bool(finding.out_of_scope),
            "duplicate": bool(finding.duplicate),
            "ai_status": ai_status,
            "ai_title": ai.title if ai else "",
            "ai_reasoning": ai.summary if ai else "",
            "ai_references": ai_references,
        }
        columns = list(row.keys())

        if fmt == "xlsx":
            wb = Workbook()
            ws = wb.active
            ws.title = "Finding"
            ws.append(columns)
            ws.append([row.get(column, "") for column in columns])
            buffer = BytesIO()
            wb.save(buffer)
            response = HttpResponse(
                buffer.getvalue(),
                content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )
            response["Content-Disposition"] = f'attachment; filename="aist_finding_{finding.id}.xlsx"'
            return response

        csv_buffer = StringIO()
        writer = csv.writer(csv_buffer)
        writer.writerow(columns)
        writer.writerow([row.get(column, "") for column in columns])
        response = HttpResponse(csv_buffer.getvalue(), content_type="text/csv; charset=utf-8")
        response["Content-Disposition"] = f'attachment; filename="aist_finding_{finding.id}.csv"'
        return response
