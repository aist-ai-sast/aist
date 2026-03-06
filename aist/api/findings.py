from __future__ import annotations

import csv
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from io import BytesIO, StringIO
from urllib.parse import urlsplit

from django.db import OperationalError, transaction
from django.http import HttpResponse
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django_filters import rest_framework as django_filters
from dojo.api_v2 import serializers as dojo_serializers
from dojo.authorization.roles_permissions import Permissions
from dojo.filters import ApiFindingFilter
from dojo.finding import helper as finding_helper
from dojo.models import Notes, Risk_Acceptance
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, OpenApiResponse, extend_schema, extend_schema_field
from openpyxl import Workbook
from rest_framework import serializers, status
from rest_framework.pagination import LimitOffsetPagination
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from aist.api.common import API_SEVERITY_VALUES
from aist.api.finding_event_stream import FindingEventStream
from aist.api.query import AuthorizedQuerySetMixin, AuthorizedQuerysetSpec
from aist.api.schema import AISTApiTag
from aist.findings_bulk_lock import (
    acquire_bulk_locks,
    get_locked_finding_ids,
    normalize_finding_ids,
    release_bulk_locks,
)
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
    is_regression = serializers.SerializerMethodField()

    @extend_schema_field(OpenApiTypes.BOOL)
    def get_is_regression(self, obj) -> bool:
        annotation = getattr(obj, "aist_annotation", None)
        return bool(annotation and annotation.is_regression)

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
    created_gte = django_filters.IsoDateTimeFilter(method="filter_created_gte")
    created_lte = django_filters.IsoDateTimeFilter(method="filter_created_lte")
    status_updated_gte = django_filters.IsoDateTimeFilter(method="filter_status_updated_gte")
    status_updated_lte = django_filters.IsoDateTimeFilter(method="filter_status_updated_lte")
    processed_gte = django_filters.IsoDateTimeFilter(method="filter_processed_gte")
    processed_lte = django_filters.IsoDateTimeFilter(method="filter_processed_lte")
    mitigated_gte = django_filters.IsoDateTimeFilter(method="filter_mitigated_gte")
    mitigated_lte = django_filters.IsoDateTimeFilter(method="filter_mitigated_lte")
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

    def _is_date_only_bound(self, key: str) -> bool:
        raw_value = (self.data.get(key) or "").strip()
        return bool(raw_value) and "T" not in raw_value and " " not in raw_value

    def _normalize_datetime_bound(self, key: str, value: datetime, *, upper: bool) -> datetime:
        if not self._is_date_only_bound(key):
            return value
        tz = value.tzinfo or timezone.get_current_timezone()
        bound_time = time.max if upper else time.min
        bound_value = datetime.combine(value.date(), bound_time)
        return timezone.make_aware(bound_value, tz) if timezone.is_naive(bound_value) else bound_value

    def filter_created_gte(self, queryset, name, value):
        if not value:
            return queryset
        value = self._normalize_datetime_bound("created_gte", value, upper=False)
        return queryset.filter(created__gte=value)

    def filter_created_lte(self, queryset, name, value):
        if not value:
            return queryset
        value = self._normalize_datetime_bound("created_lte", value, upper=True)
        return queryset.filter(created__lte=value)

    def filter_status_updated_gte(self, queryset, name, value):
        if not value:
            return queryset
        value = self._normalize_datetime_bound("status_updated_gte", value, upper=False)
        return queryset.filter(last_status_update__gte=value)

    def filter_status_updated_lte(self, queryset, name, value):
        if not value:
            return queryset
        value = self._normalize_datetime_bound("status_updated_lte", value, upper=True)
        return queryset.filter(last_status_update__lte=value)

    def _resolve_processed_bounds(
        self,
        *,
        lower: datetime | None = None,
        upper: datetime | None = None,
    ) -> tuple[datetime, datetime]:
        form_data = getattr(self, "form", None)
        cleaned = getattr(form_data, "cleaned_data", {}) if form_data is not None else {}
        lower_value = lower or cleaned.get("processed_gte")
        upper_value = upper or cleaned.get("processed_lte")
        if lower_value is None:
            lower_value = timezone.now() - timedelta(days=3650)
        if upper_value is None:
            upper_value = timezone.now() + timedelta(days=1)
        lower_value = self._normalize_datetime_bound("processed_gte", lower_value, upper=False)
        upper_value = self._normalize_datetime_bound("processed_lte", upper_value, upper=True)
        return lower_value, upper_value

    def _filter_processed_between(self, queryset, *, lower: datetime | None = None, upper: datetime | None = None):
        start, end = self._resolve_processed_bounds(lower=lower, upper=upper)
        if end <= start:
            return queryset.none()
        stream = FindingEventStream(findings=queryset, tzinfo=timezone.get_current_timezone())
        finding_ids = stream.processed_finding_ids(start=start, end=end)
        if not finding_ids:
            return queryset.none()
        return queryset.filter(id__in=finding_ids)

    def filter_processed_gte(self, queryset, name, value):
        if not value:
            return queryset
        return self._filter_processed_between(
            queryset,
            lower=self._normalize_datetime_bound("processed_gte", value, upper=False),
        )

    def filter_processed_lte(self, queryset, name, value):
        if not value:
            return queryset
        return self._filter_processed_between(
            queryset,
            upper=self._normalize_datetime_bound("processed_lte", value, upper=True),
        )

    def filter_mitigated_gte(self, queryset, name, value):
        if not value:
            return queryset
        value = self._normalize_datetime_bound("mitigated_gte", value, upper=False)
        return queryset.filter(mitigated__gte=value)

    def filter_mitigated_lte(self, queryset, name, value):
        if not value:
            return queryset
        value = self._normalize_datetime_bound("mitigated_lte", value, upper=True)
        return queryset.filter(mitigated__lte=value)

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
        tags=[AISTApiTag.FINDINGS.value],
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
            OpenApiParameter(name="status_updated_gte", required=False, type=str),
            OpenApiParameter(name="status_updated_lte", required=False, type=str),
            OpenApiParameter(name="processed_gte", required=False, type=str),
            OpenApiParameter(name="processed_lte", required=False, type=str),
            OpenApiParameter(name="mitigated_gte", required=False, type=str),
            OpenApiParameter(name="mitigated_lte", required=False, type=str),
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
            .prefetch_related("tags", "aist_project_versions", "aist_annotation")
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
        tags=[AISTApiTag.FINDINGS.value],
        summary="List finding notes",
        responses={200: AISTFindingNoteSerializer(many=True)},
    )
    def get(self, request, finding_id: int):
        finding = self.get_authorized_object(id=finding_id)
        notes = finding.notes.select_related("author").all().order_by("-date")
        out = AISTFindingNoteSerializer(notes, many=True)
        return Response(out.data, status=status.HTTP_200_OK)

    @extend_schema(
        tags=[AISTApiTag.FINDINGS.value],
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
        tags=[AISTApiTag.FINDINGS.value],
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
            .prefetch_related("tags", "aist_project_versions", "aist_annotation"),
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


class AISTFindingRiskApprovalRequestSerializer(serializers.Serializer):
    max_justification_length = 4096

    justification = serializers.CharField(
        allow_blank=False,
        trim_whitespace=True,
        max_length=max_justification_length,
    )
    accepted_by = serializers.CharField(
        required=False,
        allow_blank=True,
        trim_whitespace=True,
        max_length=200,
    )
    expiration_date = serializers.DateField(required=False, allow_null=True)
    reactivate_expired = serializers.BooleanField(required=False, default=True)


class AISTRiskApprovalCurrentSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    accepted_by = serializers.CharField(allow_blank=True, default="")
    expiration_date = serializers.DateField(allow_null=True)
    reactivate_expired = serializers.BooleanField()
    decision_details = serializers.CharField(allow_blank=True, default="")
    created = serializers.DateTimeField()


class AISTRiskApprovalStatusSerializer(serializers.Serializer):
    enabled = serializers.BooleanField()
    current = AISTRiskApprovalCurrentSerializer(allow_null=True)


class AISTFindingRiskApprovalAPI(AuthorizedQuerySetMixin, APIView):
    permission_classes = [IsAuthenticated]
    serializer_class = AISTFindingRiskApprovalRequestSerializer
    authorized_queryset = AuthorizedQuerysetSpec(
        getter=get_authorized_findings,
        permission=Permissions.Risk_Acceptance,
    )

    @extend_schema(
        tags=[AISTApiTag.FINDINGS.value],
        summary="Get risk approval status for finding",
        responses={200: AISTRiskApprovalStatusSerializer},
    )
    def get(self, request, finding_id: int):
        finding = self.get_authorized_object(id=finding_id)
        enabled = finding.test.engagement.product.enable_full_risk_acceptance
        risk_acceptance = Risk_Acceptance.objects.filter(accepted_findings=finding).first()
        current = None
        if risk_acceptance:
            current = {
                "id": risk_acceptance.id,
                "accepted_by": risk_acceptance.accepted_by or "",
                "expiration_date": risk_acceptance.expiration_date.date() if risk_acceptance.expiration_date else None,
                "reactivate_expired": risk_acceptance.reactivate_expired,
                "decision_details": risk_acceptance.decision_details or "",
                "created": risk_acceptance.created,
            }
        return Response(AISTRiskApprovalStatusSerializer({"enabled": enabled, "current": current}).data)

    @extend_schema(
        tags=[AISTApiTag.FINDINGS.value],
        summary="Create risk approval for finding",
        request=AISTFindingRiskApprovalRequestSerializer,
        responses={201: dojo_serializers.RiskAcceptanceSerializer},
    )
    def post(self, request, finding_id: int):
        finding = self.get_authorized_object(id=finding_id)
        input_serializer = self.serializer_class(data=request.data)
        input_serializer.is_valid(raise_exception=True)

        accepted_by = input_serializer.validated_data.get("accepted_by") or request.user.get_username()
        expiration_date = input_serializer.validated_data.get("expiration_date")
        expiration_date_dt = None
        if expiration_date:
            expiration_date_dt = timezone.make_aware(datetime.combine(expiration_date, time.max))

        risk_payload = {
            "name": f"AIST Risk Approval for finding #{finding.id}",
            "recommendation": Risk_Acceptance.TREATMENT_ACCEPT,
            "decision": Risk_Acceptance.TREATMENT_ACCEPT,
            "decision_details": input_serializer.validated_data["justification"],
            "accepted_by": accepted_by,
            "owner": request.user.id,
            "accepted_findings": [finding.id],
            "reactivate_expired": input_serializer.validated_data.get("reactivate_expired", True),
            "restart_sla_expired": False,
            "expiration_date": expiration_date_dt,
        }
        serializer = dojo_serializers.RiskAcceptanceSerializer(
            data=risk_payload,
            context={"request": request},
        )
        serializer.is_valid(raise_exception=True)
        risk_acceptance = serializer.save()

        return Response(
            dojo_serializers.RiskAcceptanceSerializer(
                risk_acceptance,
                context={"request": request},
            ).data,
            status=status.HTTP_201_CREATED,
        )

    @extend_schema(
        tags=[AISTApiTag.FINDINGS.value],
        summary="Revoke risk approval for finding",
        responses={204: None, 404: None},
    )
    def delete(self, request, finding_id: int):
        finding = self.get_authorized_object(id=finding_id)
        risk_acceptance = Risk_Acceptance.objects.filter(accepted_findings=finding).first()
        if not risk_acceptance:
            return Response(status=status.HTTP_404_NOT_FOUND)

        risk_acceptance.accepted_findings.remove(finding)
        finding.risk_accepted = False
        finding.active = True
        finding.save(update_fields=["risk_accepted", "active"])

        if not risk_acceptance.accepted_findings.exists():
            risk_acceptance.delete()

        return Response(status=status.HTTP_204_NO_CONTENT)


class AISTFindingBulkStatusRequestSerializer(serializers.Serializer):
    max_batch_size = 500
    max_reason_length = 4096

    finding_ids = serializers.ListField(
        child=serializers.IntegerField(min_value=1),
        allow_empty=False,
        max_length=max_batch_size,
    )
    action = serializers.ChoiceField(choices=("close", "reopen"))
    close_reason = serializers.ChoiceField(
        choices=("mitigated", "false_positive", "out_of_scope", "duplicate"),
        required=False,
        allow_null=True,
    )
    reason = serializers.CharField(allow_blank=False, trim_whitespace=True, max_length=max_reason_length)

    def validate(self, attrs):
        action = attrs.get("action")
        close_reason = attrs.get("close_reason")
        if action == "close" and not close_reason:
            msg = "close_reason is required when action=close"
            raise serializers.ValidationError({"close_reason": msg})
        return attrs


class AISTFindingBulkStatusAPI(AuthorizedQuerySetMixin, APIView):
    permission_classes = [IsAuthenticated]
    serializer_class = AISTFindingBulkStatusRequestSerializer
    authorized_queryset = AuthorizedQuerysetSpec(
        getter=get_authorized_findings,
        permission=Permissions.Finding_Edit,
    )

    @extend_schema(
        tags=[AISTApiTag.FINDINGS.value],
        summary="Bulk change finding status",
        request=AISTFindingBulkStatusRequestSerializer,
        responses={200: OpenApiTypes.OBJECT},
    )
    def post(self, request):
        serializer = self.serializer_class(data=request.data)
        serializer.is_valid(raise_exception=True)
        requested_ids = normalize_finding_ids(serializer.validated_data["finding_ids"])
        if not requested_ids:
            return Response(
                {"detail": "finding_ids must contain at least one valid id."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # --- Authorization pre-check (no row lock yet) ---
        # Verify all findings are accessible before acquiring any markers so we
        # never mark findings the user cannot edit.
        authorized_ids = set(
            self.get_authorized_queryset()
            .filter(id__in=requested_ids)
            .values_list("id", flat=True),
        )
        if len(authorized_ids) != len(requested_ids):
            return Response(
                {"detail": "Some findings are unavailable."},
                status=status.HTTP_404_NOT_FOUND,
            )

        # --- UX pre-check (read-only cache query) ---
        # Fast early exit that returns the specific IDs currently in-flight so
        # the UI can highlight them.  Not an integrity guarantee — the DB lock
        # below is.
        known_locked = get_locked_finding_ids(requested_ids)
        if known_locked:
            return Response(
                {"detail": "Some findings are currently locked.", "locked_ids": sorted(known_locked)},
                status=status.HTTP_423_LOCKED,
            )

        # --- Mark findings as in-flight (UX marker) ---
        # Lets the middleware return an early 423 to concurrent single-finding
        # mutations and lets the UI show a locked state.  Released in finally.
        owner_token = str(uuid.uuid4())
        acquired_ids, _ = acquire_bulk_locks(requested_ids, owner_token)

        action = serializer.validated_data["action"]
        close_reason = serializer.validated_data.get("close_reason")
        reason_note = serializer.validated_data["reason"]
        bulk_note_entry = f"Bulk status update: {reason_note}"
        updated_ids: list[int] = []
        try:
            with transaction.atomic():
                # select_for_update(nowait=True) is the actual integrity guard:
                # - raises OperationalError immediately if any row is already
                #   locked by another transaction (bulk-vs-bulk race).
                # - a count mismatch after the lock means a finding was deleted
                #   or became inaccessible between the pre-check and now.
                findings = list(
                    self.get_authorized_queryset()
                    .select_for_update(nowait=True)
                    .select_related("test__engagement__product")
                    .filter(id__in=requested_ids),
                )
                if len(findings) != len(requested_ids):
                    return Response(
                        {"detail": "Some findings were modified concurrently. Please retry."},
                        status=status.HTTP_409_CONFLICT,
                    )
                for finding in findings:
                    if action == "reopen":
                        _bulk_reopen_finding(
                            finding=finding,
                            user=request.user,
                            note_entry=bulk_note_entry,
                        )
                    else:
                        _bulk_close_finding(
                            finding=finding,
                            user=request.user,
                            close_reason=close_reason or "mitigated",
                            note_entry=bulk_note_entry,
                        )
                    updated_ids.append(finding.id)
        except OperationalError:
            # Another DB transaction already holds a row lock on one or more
            # findings.  The cache pre-check handles the common case; this
            # catches the narrow race window between that check and the DB lock.
            return Response(
                {"detail": "Some findings are locked by a concurrent operation. Please retry."},
                status=status.HTTP_423_LOCKED,
            )
        finally:
            release_bulk_locks(acquired_ids, owner_token)

        return Response(
            {"updated_count": len(updated_ids), "updated_ids": updated_ids},
            status=status.HTTP_200_OK,
        )


def _bulk_reopen_finding(*, finding, user, note_entry: str) -> None:
    finding.active = True
    finding.is_mitigated = False
    finding.false_p = False
    finding.out_of_scope = False
    finding.duplicate = False
    finding.under_review = False
    finding.last_reviewed = timezone.now()
    finding.last_reviewed_by = user
    finding.save()
    _add_bulk_note(finding=finding, user=user, note_entry=note_entry)


def _bulk_close_finding(*, finding, user, close_reason: str, note_entry: str) -> None:
    finding_helper.close_finding(
        finding=finding,
        user=user,
        is_mitigated=close_reason == "mitigated",
        mitigated=timezone.now(),
        mitigated_by=user,
        false_p=close_reason == "false_positive",
        out_of_scope=close_reason == "out_of_scope",
        duplicate=close_reason == "duplicate",
        note_entry=note_entry,
    )
    finding.save()


def _add_bulk_note(*, finding, user, note_entry: str) -> None:
    note = Notes.objects.create(entry=note_entry, author=user, private=False)
    finding.notes.add(note)
