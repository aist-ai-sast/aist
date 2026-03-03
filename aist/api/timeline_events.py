from __future__ import annotations

from datetime import timedelta

from django.contrib.auth import get_user_model
from django.db.models import TextField
from django.db.models.functions import Cast
from django.urls import reverse
from dojo.authorization.roles_permissions import Permissions
from dojo.models import Finding
from dojo.pghistory_models import DojoEvents
from drf_spectacular.utils import OpenApiParameter, OpenApiResponse, extend_schema
from rest_framework import serializers, status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from aist.api.calendar_domain import build_calendar_request_context
from aist.api.common import CommaSeparatedListField, TimezoneNameField
from aist.api.finding_event_stream import FindingEventStream
from aist.api.schema import AISTApiTag
from aist.queries import get_authorized_findings

TIMELINE_EVENT_TYPES = ("finding_created", "finding_processed", "finding_note_added")
MAX_TIMELINE_RANGE_DAYS = 93
MAX_FINDING_HISTORY_RANGE_DAYS = 3650
STATUS_DIFF_KEYS = frozenset({"active", "is_mitigated", "false_p", "out_of_scope", "duplicate"})
SYSTEM_OWNER = "System"
HISTORY_DETAIL_BY_EVENT_TYPE = {
    "finding_created": "Finding created",
    "finding_note_added": "Comment added",
}
HISTORY_DETAIL_BY_PROCESSED_REASON = {
    "severity_changed": "Severity changed",
    "mitigated": "Processed as mitigated",
    "false_positive": "Processed as false positive",
    "out_of_scope": "Processed as out of scope",
    "duplicate": "Processed as duplicate",
    "resolved": "Processed as resolved",
}


class FindingTimelineQuerySerializer(serializers.Serializer):
    start = serializers.DateTimeField(required=True)
    end = serializers.DateTimeField(required=True)
    timezone = TimezoneNameField(required=False, allow_blank=True)
    project_id = CommaSeparatedListField(
        child=serializers.IntegerField(min_value=1),
        required=False,
    )
    event_types = CommaSeparatedListField(
        child=serializers.ChoiceField(choices=TIMELINE_EVENT_TYPES),
        required=False,
    )
    finding_id = serializers.PrimaryKeyRelatedField(
        queryset=Finding.objects.none(),
        required=False,
    )
    limit = serializers.IntegerField(default=500, min_value=1, max_value=2000, required=False)

    def get_fields(self):
        fields = super().get_fields()
        request = self.context.get("request")
        if request and getattr(request, "user", None) and request.user.is_authenticated:
            fields["finding_id"].queryset = get_authorized_findings(
                Permissions.Finding_View,
                user=request.user,
            )
        return fields

    def validate(self, attrs):
        if attrs["end"] <= attrs["start"]:
            msg = "end must be greater than start"
            raise serializers.ValidationError({"end": msg})
        max_days = MAX_FINDING_HISTORY_RANGE_DAYS if attrs.get("finding_id") else MAX_TIMELINE_RANGE_DAYS
        if attrs["end"] - attrs["start"] > timedelta(days=max_days):
            msg = f"requested range must not exceed {max_days} days"
            raise serializers.ValidationError({"end": msg})
        attrs["event_types"] = tuple(dict.fromkeys(attrs.get("event_types", TIMELINE_EVENT_TYPES)))
        attrs["project_id"] = list(dict.fromkeys(attrs.get("project_id", [])))
        return attrs


class FindingTimelineRowSerializer(serializers.Serializer):
    id = serializers.CharField()
    event_type = serializers.ChoiceField(choices=TIMELINE_EVENT_TYPES)
    happened_at = serializers.DateTimeField()
    finding_id = serializers.IntegerField()
    title = serializers.CharField()
    severity = serializers.CharField()
    project_ids = serializers.ListField(child=serializers.IntegerField(min_value=1))
    processed_reason = serializers.CharField(required=False, allow_null=True)
    owner = serializers.CharField(required=False, allow_blank=True, allow_null=True)
    details = serializers.CharField(required=False, allow_blank=True, allow_null=True)
    link = serializers.CharField()


class AISTFindingTimelineAPI(APIView):
    permission_classes = [IsAuthenticated]

    @staticmethod
    def _render_owner(user_row: dict[str, str | None] | None) -> str:
        if not user_row:
            return SYSTEM_OWNER
        full_name = " ".join(
            [part for part in [user_row.get("first_name"), user_row.get("last_name")] if part],
        ).strip()
        return full_name or (user_row.get("username") or SYSTEM_OWNER)

    @staticmethod
    def _is_status_update(diff: dict) -> bool:
        return bool(STATUS_DIFF_KEYS.intersection(diff.keys()))

    def _build_event_owner_index(self, *, findings, start, end) -> dict[tuple[str, int, int], int | None]:
        finding_ids = findings.annotate(id_text=Cast("id", output_field=TextField())).values("id_text")
        event_rows = list(
            DojoEvents.objects.filter(
                pgh_obj_model__endswith=".Finding",
                pgh_obj_id__in=finding_ids,
                pgh_created_at__gte=start,
                pgh_created_at__lt=end,
            )
            .values("pgh_obj_id", "pgh_label", "pgh_created_at", "pgh_diff", "user")
            .order_by("-pgh_created_at", "-pgh_id"),
        )
        owner_index: dict[tuple[str, int, int], int | None] = {}
        for row in event_rows:
            finding_id_raw = row.get("pgh_obj_id")
            happened_at = row.get("pgh_created_at")
            if finding_id_raw is None or happened_at is None:
                continue
            finding_id = int(finding_id_raw)
            ts = int(happened_at.timestamp())
            diff = row.get("pgh_diff") or {}
            label = str(row.get("pgh_label") or "")
            user_id = row.get("user")
            if label == "insert":
                owner_index["finding_created", finding_id, ts] = user_id
                continue
            if not isinstance(diff, dict):
                continue
            if "severity" in diff:
                owner_index["finding_processed", finding_id, ts] = user_id
                continue
            if self._is_status_update(diff):
                owner_index["finding_processed", finding_id, ts] = user_id
        return owner_index

    @staticmethod
    def _history_details(event_type: str, processed_reason: str | None) -> str:
        event_level = HISTORY_DETAIL_BY_EVENT_TYPE.get(event_type)
        if event_level:
            return event_level
        if processed_reason:
            reason_level = HISTORY_DETAIL_BY_PROCESSED_REASON.get(processed_reason)
            if reason_level:
                return reason_level
        return "Finding updated"

    def _note_events(self, *, finding, allowed_types: set[str], limit: int) -> list[dict]:
        if "finding_note_added" not in allowed_types:
            return []
        notes = (
            finding.notes.select_related("author")
            .all()
            .order_by("-date")[:limit]
        )
        payload = []
        for note in notes:
            happened_at = note.date
            if not happened_at:
                continue
            owner = self._render_owner({
                "username": getattr(getattr(note, "author", None), "username", None),
                "first_name": getattr(getattr(note, "author", None), "first_name", None),
                "last_name": getattr(getattr(note, "author", None), "last_name", None),
            })
            payload.append(
                {
                    "id": f"finding_note_added:{finding.id}:{note.id}",
                    "event_type": "finding_note_added",
                    "happened_at": happened_at.isoformat(),
                    "finding_id": finding.id,
                    "title": finding.title,
                    "severity": finding.severity or "",
                    "project_ids": [],
                    "processed_reason": None,
                    "owner": owner,
                    "details": (note.entry or "").strip(),
                    "link": reverse("finding-detail", args=[finding.id]),
                },
            )
        return payload

    def _resolve_row_owner(
        self,
        *,
        row,
        owner_index: dict[tuple[str, int, int], int | None],
        user_map: dict[int, dict[str, str | None]],
        reporter_index: dict[int, str],
    ) -> str:
        key = (row.event_type, row.finding_id, int(row.happened_at.timestamp()))
        owner_user_id = int(owner_index.get(key, 0) or 0)
        if owner_user_id:
            return self._render_owner(user_map.get(owner_user_id))
        if row.event_type == "finding_created":
            return reporter_index.get(row.finding_id, SYSTEM_OWNER)
        return SYSTEM_OWNER

    @extend_schema(
        tags=[AISTApiTag.FINDINGS.value],
        operation_id="aist_finding_timeline_list",
        summary="List finding timeline events for client UI",
        parameters=[
            OpenApiParameter(name="start", required=True, type=str),
            OpenApiParameter(name="end", required=True, type=str),
            OpenApiParameter(name="timezone", required=False, type=str),
            OpenApiParameter(name="project_id", required=False, type=int, many=True),
            OpenApiParameter(name="event_types", required=False, type=str, many=True, enum=TIMELINE_EVENT_TYPES),
            OpenApiParameter(name="finding_id", required=False, type=int),
            OpenApiParameter(name="limit", required=False, type=int),
        ],
        responses={200: OpenApiResponse(response=FindingTimelineRowSerializer(many=True))},
    )
    def get(self, request, *args, **kwargs):
        params = FindingTimelineQuerySerializer(
            data=request.query_params,
            context={"request": request},
        )
        if not params.is_valid():
            return Response(params.errors, status=status.HTTP_400_BAD_REQUEST)
        authorized_findings = params.fields["finding_id"].queryset

        context = build_calendar_request_context(
            {
                "start": params.validated_data["start"],
                "end": params.validated_data["end"],
                "view": "month",
                "grouping": "none",
                "limit": params.validated_data["limit"],
                "event_types": ["finding_created"],
                "project_id": params.validated_data["project_id"],
                "timezone": params.validated_data.get("timezone", ""),
            },
        )

        findings = authorized_findings
        if context.project_ids:
            findings = findings.filter(aist_project_versions__project_id__in=context.project_ids).distinct()

        finding_obj = params.validated_data.get("finding_id")
        finding = None
        if finding_obj:
            findings = findings.filter(id=finding_obj.id)
            finding = findings.first()

        stream = FindingEventStream(findings=findings, tzinfo=context.tzinfo)
        rows = stream.timeline_rows(start=context.start, end=context.end, limit=context.limit + 1)
        allowed_types = set(params.validated_data["event_types"])
        owner_index = self._build_event_owner_index(findings=findings, start=context.start, end=context.end)
        row_finding_ids = {int(row.finding_id) for row in rows}
        reporter_index: dict[int, str] = {}
        if row_finding_ids:
            reporter_index = {
                int(row["id"]): self._render_owner(row)
                for row in findings.filter(id__in=row_finding_ids).values(
                    "id",
                    "reporter__username",
                    "reporter__first_name",
                    "reporter__last_name",
                )
                if row.get("id")
            }
        owner_ids = {owner for owner in owner_index.values() if owner}
        user_map: dict[int, dict[str, str | None]] = {}
        if owner_ids:
            user_model = get_user_model()
            user_map = {
                int(row["id"]): row
                for row in user_model.objects.filter(id__in=owner_ids).values("id", "username", "first_name", "last_name")
            }

        payload = []
        for row in rows:
            if row.event_type not in allowed_types:
                continue
            payload.append(
                {
                    "id": f"{row.event_type}:{row.finding_id}:{int(row.happened_at.timestamp())}",
                    "event_type": row.event_type,
                    "happened_at": row.happened_at.isoformat(),
                    "finding_id": row.finding_id,
                    "title": row.title,
                    "severity": row.severity,
                    "project_ids": row.project_ids,
                    "processed_reason": row.processed_reason,
                    "owner": self._resolve_row_owner(
                        row=row,
                        owner_index=owner_index,
                        user_map=user_map,
                        reporter_index=reporter_index,
                    ),
                    "details": self._history_details(row.event_type, row.processed_reason),
                    "link": reverse("finding-detail", args=[row.finding_id]),
                },
            )
        if finding:
            payload.extend(self._note_events(finding=finding, allowed_types=allowed_types, limit=context.limit + 1))
            payload.sort(
                key=lambda item: (item.get("happened_at", ""), item.get("id", "")),
                reverse=True,
            )
        truncated = len(payload) > context.limit
        return Response(
            {
                "range": {
                    "start": context.start.isoformat(),
                    "end": context.end.isoformat(),
                    "timezone": str(context.tzinfo),
                },
                "events": payload[: context.limit],
                "meta": {"total": len(payload[: context.limit]), "truncated": truncated},
            },
            status=status.HTTP_200_OK,
        )
