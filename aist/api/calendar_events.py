from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from operator import itemgetter
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from croniter import croniter
from django.db.models import Count, DateTimeField
from django.db.models.functions import Coalesce
from django.utils import timezone
from dojo.authorization.roles_permissions import Permissions
from dojo.models import Finding
from drf_spectacular.utils import OpenApiParameter, OpenApiResponse, extend_schema
from rest_framework import serializers, status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from aist.queries import (
    get_authorized_aist_launch_schedules,
    get_authorized_aist_pipelines,
    get_authorized_aist_projects,
    get_authorized_findings,
)
from aist.utils.project_version_refs import resolve_project_version_git_refs


@dataclass(frozen=True, slots=True)
class CalendarApiChoices:
    view: list[str]
    grouping: list[str]
    event_type: list[str]


CALENDAR_API_CHOICES = CalendarApiChoices(
    view=["day", "week", "month"],
    grouping=["auto", "none"],
    event_type=["pipeline_started", "pipeline_scheduled", "finding_created", "finding_mitigated", "project_created"],
)

MAX_RANGE_DAYS = 93
MAX_SCHEDULE_OCCURRENCES_PER_SCHEDULE = 300


def _split_csv_or_repeated(values: list[str]) -> list[str]:
    out: list[str] = []
    for raw in values:
        for part in (raw or "").split(","):
            normalized = part.strip()
            if normalized:
                out.append(normalized)
    return out


def _ensure_aware(value: datetime, tzinfo) -> datetime:
    if timezone.is_naive(value):
        return timezone.make_aware(value, tzinfo)
    return value


def _build_calendar_link(path: str, params: dict[str, str | int] | None = None) -> str:
    if not params:
        return path
    query = "&".join(f"{key}={value}" for key, value in params.items())
    return f"{path}?{query}"


class CalendarEventRowSerializer(serializers.Serializer):
    id = serializers.CharField()
    event_type = serializers.ChoiceField(choices=CALENDAR_API_CHOICES.event_type)
    title = serializers.CharField()
    start = serializers.DateTimeField()
    end = serializers.DateTimeField(allow_null=True)
    is_all_day = serializers.BooleanField()
    is_aggregated = serializers.BooleanField()
    count = serializers.IntegerField(min_value=1)
    is_future = serializers.BooleanField()
    color_variant = serializers.CharField()
    summary = serializers.JSONField()
    link = serializers.CharField(allow_null=True, allow_blank=True)


class CalendarEventsQuerySerializer(serializers.Serializer):
    start = serializers.DateTimeField(required=True)
    end = serializers.DateTimeField(required=True)
    view = serializers.ChoiceField(choices=CALENDAR_API_CHOICES.view, required=True)
    timezone = serializers.CharField(required=False, allow_blank=True)
    grouping = serializers.ChoiceField(
        choices=CALENDAR_API_CHOICES.grouping,
        default="auto",
        required=False,
    )
    limit = serializers.IntegerField(default=500, min_value=1, max_value=2000, required=False)
    event_types = serializers.ListField(
        child=serializers.ChoiceField(choices=CALENDAR_API_CHOICES.event_type),
        required=False,
    )
    project_id = serializers.ListField(
        child=serializers.IntegerField(min_value=1),
        required=False,
    )

    def validate(self, attrs):
        if attrs["end"] <= attrs["start"]:
            msg = "end must be greater than start"
            raise serializers.ValidationError({"end": msg})
        if attrs["end"] - attrs["start"] > timedelta(days=MAX_RANGE_DAYS):
            msg = f"requested range must not exceed {MAX_RANGE_DAYS} days"
            raise serializers.ValidationError({"end": msg})
        attrs["event_types"] = list(dict.fromkeys(attrs.get("event_types", CALENDAR_API_CHOICES.event_type)))
        attrs["project_id"] = list(dict.fromkeys(attrs.get("project_id", [])))
        return attrs


class CalendarEventDetailQuerySerializer(serializers.Serializer):
    timezone = serializers.CharField(required=False, allow_blank=True)
    project_id = serializers.ListField(
        child=serializers.IntegerField(min_value=1),
        required=False,
    )

    def validate(self, attrs):
        attrs["project_id"] = list(dict.fromkeys(attrs.get("project_id", [])))
        return attrs


class AISTCalendarEventsAPI(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(
        tags=["aist"],
        summary="List calendar events for client UI",
        parameters=[
            OpenApiParameter(name="start", required=True, type=str),
            OpenApiParameter(name="end", required=True, type=str),
            OpenApiParameter(name="view", required=True, type=str, enum=CALENDAR_API_CHOICES.view),
            OpenApiParameter(name="event_types", required=False, type=str, many=True, enum=CALENDAR_API_CHOICES.event_type),
            OpenApiParameter(name="project_id", required=False, type=int, many=True),
            OpenApiParameter(name="timezone", required=False, type=str),
            OpenApiParameter(name="grouping", required=False, type=str, enum=CALENDAR_API_CHOICES.grouping),
            OpenApiParameter(name="limit", required=False, type=int),
        ],
        responses={200: OpenApiResponse(response=CalendarEventRowSerializer(many=True))},
    )
    def get(self, request, *args, **kwargs):
        payload = {
            "start": request.query_params.get("start"),
            "end": request.query_params.get("end"),
            "view": request.query_params.get("view"),
            "grouping": request.query_params.get("grouping") or "auto",
        }
        if request.query_params.get("timezone") is not None:
            payload["timezone"] = request.query_params.get("timezone")
        if request.query_params.get("limit") is not None:
            payload["limit"] = request.query_params.get("limit")
        event_types = _split_csv_or_repeated(request.query_params.getlist("event_types"))
        if event_types:
            payload["event_types"] = event_types
        project_ids = _split_csv_or_repeated(request.query_params.getlist("project_id"))
        if project_ids:
            payload["project_id"] = project_ids

        params = CalendarEventsQuerySerializer(data=payload)
        if not params.is_valid():
            return Response(params.errors, status=status.HTTP_400_BAD_REQUEST)
        data = params.validated_data

        tz_name = (data.get("timezone") or "").strip()
        if tz_name:
            try:
                tzinfo = ZoneInfo(tz_name)
            except ZoneInfoNotFoundError:
                return Response({"timezone": ["Invalid timezone."]}, status=status.HTTP_400_BAD_REQUEST)
        else:
            tzinfo = timezone.get_current_timezone()

        range_start = data["start"]
        range_end = data["end"]
        now_local = timezone.localtime(timezone.now(), tzinfo)
        event_types_filter = set(data["event_types"])
        project_filter = set(data.get("project_id", []))

        projects = get_authorized_aist_projects(Permissions.Product_View, user=request.user).select_related("product")
        pipelines = get_authorized_aist_pipelines(Permissions.Product_View, user=request.user).select_related(
            "project",
            "project__product",
        )
        schedules = get_authorized_aist_launch_schedules(Permissions.Product_View, user=request.user).select_related(
            "launch_config__project",
            "launch_config__project__product",
        )
        findings = get_authorized_findings(Permissions.Finding_View, user=request.user)

        if project_filter:
            projects = projects.filter(id__in=project_filter)
            pipelines = pipelines.filter(project_id__in=project_filter)
            schedules = schedules.filter(launch_config__project_id__in=project_filter)
            findings = findings.filter(aist_project_versions__project_id__in=project_filter).distinct()

        events: list[dict] = []

        if "project_created" in event_types_filter:
            project_rows = projects.filter(created__gte=range_start, created__lt=range_end).order_by("created")
            for item in project_rows:
                started = timezone.localtime(item.created, tzinfo)
                is_future = started > now_local
                events.append(
                    {
                        "id": f"project_created:{item.id}",
                        "event_type": "project_created",
                        "title": f"Project created: {item.product.name}",
                        "start": started,
                        "end": None,
                        "is_all_day": False,
                        "is_aggregated": False,
                        "count": 1,
                        "is_future": is_future,
                        "color_variant": "future_single" if is_future else "past_single",
                        "summary": {"project_id": item.id, "project_name": item.product.name},
                        "link": "/products",
                    },
                )

        if "pipeline_started" in event_types_filter:
            pipeline_rows = list(pipelines.filter(created__gte=range_start, created__lt=range_end).order_by("created"))
            pipeline_ids = [item.id for item in pipeline_rows]
            findings_by_pipeline: dict[str, int] = {}
            if pipeline_ids:
                counts_qs = (
                    Finding.objects.filter(test__aist_pipelines__id__in=pipeline_ids)
                    .order_by()
                    .values("test__aist_pipelines__id")
                    .annotate(total=Count("id"))
                )
                findings_by_pipeline = {
                    str(row["test__aist_pipelines__id"]): int(row["total"])
                    for row in counts_qs
                }

            for item in pipeline_rows:
                started = timezone.localtime(item.created, tzinfo)
                finished = timezone.localtime(item.updated, tzinfo)
                is_future = started > now_local
                refs = resolve_project_version_git_refs(item.project_version)
                action_runs = (item.launch_data or {}).get("action_runs") or []
                actions = [
                    {
                        "source": run.get("source"),
                        "type": run.get("action_type"),
                        "status": run.get("status"),
                        "updated": run.get("updated_at"),
                    }
                    for run in action_runs
                ]
                duration_seconds = max(int((item.updated - item.created).total_seconds()), 0)
                events.append(
                    {
                        "id": f"pipeline_started:{item.id}",
                        "event_type": "pipeline_started",
                        "title": f"Pipeline started: {item.id}",
                        "start": started,
                        "end": finished if finished > started else None,
                        "is_all_day": False,
                        "is_aggregated": False,
                        "count": 1,
                        "is_future": is_future,
                        "color_variant": "future_single" if is_future else "past_single",
                        "summary": {
                            "project_id": item.project_id,
                            "project_name": item.project.product.name,
                            "pipeline_id": item.id,
                            "status": item.status,
                            "created": item.created.isoformat(),
                            "updated": item.updated.isoformat(),
                            "duration_seconds": duration_seconds,
                            "branch": refs.branch,
                            "commit": refs.commit,
                            "findings": findings_by_pipeline.get(item.id, 0),
                            "actions": actions,
                        },
                        "link": _build_calendar_link(
                            "/pipelines",
                            {
                                "project": item.project_id,
                                "created_from": item.created.date().isoformat(),
                                "created_to": item.created.date().isoformat(),
                            },
                        ),
                    },
                )

        if "pipeline_scheduled" in event_types_filter:
            range_start_local = timezone.localtime(range_start, tzinfo)
            range_end_local = timezone.localtime(range_end, tzinfo)
            for schedule in schedules.filter(enabled=True):
                occurrences = 0
                iterator = croniter(schedule.cron_expression, range_start_local)
                while occurrences < MAX_SCHEDULE_OCCURRENCES_PER_SCHEDULE:
                    next_run = _ensure_aware(iterator.get_next(datetime), tzinfo)
                    if next_run >= range_end_local:
                        break
                    is_future = next_run > now_local
                    events.append(
                        {
                            "id": f"pipeline_scheduled:{schedule.id}:{int(next_run.timestamp())}",
                            "event_type": "pipeline_scheduled",
                            "title": f"Pipeline scheduled: {schedule.launch_config.project.product.name}",
                            "start": next_run,
                            "end": None,
                            "is_all_day": False,
                            "is_aggregated": False,
                            "count": 1,
                            "is_future": is_future,
                            "color_variant": "future_single" if is_future else "past_single",
                            "summary": {
                                "schedule_id": schedule.id,
                                "project_id": schedule.launch_config.project_id,
                                "project_name": schedule.launch_config.project.product.name,
                                "cron_expression": schedule.cron_expression,
                            },
                            "link": None,
                        },
                    )
                    occurrences += 1

        if "finding_created" in event_types_filter:
            findings_qs = (
                findings.annotate(event_at=Coalesce("date", "created", output_field=DateTimeField()))
                .filter(event_at__gte=range_start, event_at__lt=range_end)
                .distinct()
            )
            if data["grouping"] == "none":
                finding_rows = findings_qs.order_by("event_at", "id")[: data["limit"]]
                for finding in finding_rows:
                    started = timezone.localtime(finding.event_at, tzinfo)
                    is_future = started > now_local
                    events.append(
                        {
                            "id": f"finding_created:{finding.id}",
                            "event_type": "finding_created",
                            "title": f"Finding created: {finding.title}",
                            "start": started,
                            "end": None,
                            "is_all_day": False,
                            "is_aggregated": False,
                            "count": 1,
                            "is_future": is_future,
                            "color_variant": "future_single" if is_future else "past_single",
                            "summary": {"finding_id": finding.id, "severity": finding.severity},
                            "link": f"/finding/{finding.id}",
                        },
                    )
            else:
                by_day: dict = defaultdict(
                    lambda: {
                        "total": 0,
                        "severity": {"Critical": 0, "High": 0, "Medium": 0, "Low": 0, "Info": 0},
                    },
                )
                finding_rows = findings_qs.values("id", "title", "severity", "event_at").order_by("event_at", "id")
                for row in finding_rows:
                    event_at = timezone.localtime(row["event_at"], tzinfo)
                    day = event_at.date()
                    bucket = by_day[day]
                    bucket["total"] += 1
                    severity = str(row.get("severity") or "")
                    if severity in bucket["severity"]:
                        bucket["severity"][severity] += 1

                for day in sorted(by_day):
                    bucket = by_day[day]
                    start_of_day = timezone.make_aware(datetime.combine(day, time.min), tzinfo)
                    is_future = start_of_day > now_local
                    events.append(
                        {
                            "id": f"finding_created:{day.isoformat()}",
                            "event_type": "finding_created",
                            "title": f"Findings created: {bucket['total']}",
                            "start": start_of_day,
                            "end": None,
                            "is_all_day": True,
                            "is_aggregated": True,
                            "count": bucket["total"],
                            "is_future": is_future,
                            "color_variant": "future_aggregate" if is_future else "past_aggregate",
                            "summary": {
                                "severity": bucket["severity"],
                            },
                            "link": None,
                        },
                    )

        if "finding_mitigated" in event_types_filter:
            mitigated_qs = (
                findings.filter(active=False)
                .exclude(last_status_update__isnull=True)
                .filter(last_status_update__gte=range_start, last_status_update__lt=range_end)
                .distinct()
            )
            by_day: dict = defaultdict(
                lambda: {
                    "total": 0,
                    "severity": {"Critical": 0, "High": 0, "Medium": 0, "Low": 0, "Info": 0},
                },
            )
            for row in mitigated_qs.values("severity", "last_status_update").order_by("last_status_update"):
                day = timezone.localtime(row["last_status_update"], tzinfo).date()
                bucket = by_day[day]
                bucket["total"] += 1
                severity = str(row.get("severity") or "")
                if severity in bucket["severity"]:
                    bucket["severity"][severity] += 1
            for day in sorted(by_day):
                bucket = by_day[day]
                start_of_day = timezone.make_aware(datetime.combine(day, time.min), tzinfo)
                is_future = start_of_day > now_local
                events.append(
                    {
                        "id": f"finding_mitigated:{day.isoformat()}",
                        "event_type": "finding_mitigated",
                        "title": f"Findings mitigated: {bucket['total']}",
                        "start": start_of_day,
                        "end": None,
                        "is_all_day": True,
                        "is_aggregated": True,
                        "count": bucket["total"],
                        "is_future": is_future,
                        "color_variant": "future_aggregate" if is_future else "past_aggregate",
                        "summary": {
                            "severity": bucket["severity"],
                            "active": False,
                        },
                        "link": None,
                    },
                )

        events.sort(key=itemgetter("start", "id"))
        truncated = len(events) > data["limit"]
        events = events[: data["limit"]]

        return Response(
            {
                "range": {
                    "start": range_start.isoformat(),
                    "end": range_end.isoformat(),
                    "timezone": str(tzinfo),
                    "view": data["view"],
                },
                "events": events,
                "meta": {"total": len(events), "truncated": truncated},
            },
            status=status.HTTP_200_OK,
        )


class AISTCalendarEventDetailAPI(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(
        tags=["aist"],
        summary="Get a single calendar event detail",
        parameters=[
            OpenApiParameter(name="event_id", required=True, location=OpenApiParameter.PATH, type=str),
            OpenApiParameter(name="project_id", required=False, type=int, many=True),
            OpenApiParameter(name="timezone", required=False, type=str),
        ],
        responses={
            200: OpenApiResponse(response=CalendarEventRowSerializer),
            404: OpenApiResponse(description="Event not found"),
        },
    )
    def get(self, request, event_id: str, *args, **kwargs):
        payload = {}
        if request.query_params.get("timezone") is not None:
            payload["timezone"] = request.query_params.get("timezone")
        project_ids = _split_csv_or_repeated(request.query_params.getlist("project_id"))
        if project_ids:
            payload["project_id"] = project_ids
        params = CalendarEventDetailQuerySerializer(data=payload)
        if not params.is_valid():
            return Response(params.errors, status=status.HTTP_400_BAD_REQUEST)
        data = params.validated_data

        tz_name = (data.get("timezone") or "").strip()
        if tz_name:
            try:
                tzinfo = ZoneInfo(tz_name)
            except ZoneInfoNotFoundError:
                return Response({"timezone": ["Invalid timezone."]}, status=status.HTTP_400_BAD_REQUEST)
        else:
            tzinfo = timezone.get_current_timezone()
        now_local = timezone.localtime(timezone.now(), tzinfo)
        project_filter = set(data.get("project_id", []))

        event_type, _, token = event_id.partition(":")
        if not token:
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)

        projects = get_authorized_aist_projects(Permissions.Product_View, user=request.user).select_related("product")
        pipelines = get_authorized_aist_pipelines(Permissions.Product_View, user=request.user).select_related(
            "project",
            "project__product",
        )
        schedules = get_authorized_aist_launch_schedules(Permissions.Product_View, user=request.user).select_related(
            "launch_config__project",
            "launch_config__project__product",
        )
        findings = get_authorized_findings(Permissions.Finding_View, user=request.user)

        if project_filter:
            projects = projects.filter(id__in=project_filter)
            pipelines = pipelines.filter(project_id__in=project_filter)
            schedules = schedules.filter(launch_config__project_id__in=project_filter)
            findings = findings.filter(aist_project_versions__project_id__in=project_filter).distinct()

        if event_type == "project_created":
            try:
                project_id = int(token)
            except ValueError:
                return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)
            item = projects.filter(id=project_id).first()
            if not item:
                return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)
            started = timezone.localtime(item.created, tzinfo)
            is_future = started > now_local
            event = {
                "id": f"project_created:{item.id}",
                "event_type": "project_created",
                "title": f"Project created: {item.product.name}",
                "start": started,
                "end": None,
                "is_all_day": False,
                "is_aggregated": False,
                "count": 1,
                "is_future": is_future,
                "color_variant": "future_single" if is_future else "past_single",
                "summary": {"project_id": item.id, "project_name": item.product.name},
                "link": "/products",
            }
            return Response(event, status=status.HTTP_200_OK)

        if event_type == "pipeline_started":
            item = pipelines.filter(id=token).first()
            if not item:
                return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)
            findings_count = (
                Finding.objects.filter(test__aist_pipelines__id=item.id)
                .order_by()
                .values("id")
                .distinct()
                .count()
            )
            started = timezone.localtime(item.created, tzinfo)
            finished = timezone.localtime(item.updated, tzinfo)
            is_future = started > now_local
            refs = resolve_project_version_git_refs(item.project_version)
            action_runs = (item.launch_data or {}).get("action_runs") or []
            actions = [
                {
                    "source": run.get("source"),
                    "type": run.get("action_type"),
                    "status": run.get("status"),
                    "updated": run.get("updated_at"),
                }
                for run in action_runs
            ]
            duration_seconds = max(int((item.updated - item.created).total_seconds()), 0)
            event = {
                "id": f"pipeline_started:{item.id}",
                "event_type": "pipeline_started",
                "title": f"Pipeline started: {item.id}",
                "start": started,
                "end": finished if finished > started else None,
                "is_all_day": False,
                "is_aggregated": False,
                "count": 1,
                "is_future": is_future,
                "color_variant": "future_single" if is_future else "past_single",
                "summary": {
                    "project_id": item.project_id,
                    "project_name": item.project.product.name,
                    "pipeline_id": item.id,
                    "status": item.status,
                    "created": item.created.isoformat(),
                    "updated": item.updated.isoformat(),
                    "duration_seconds": duration_seconds,
                    "branch": refs.branch,
                    "commit": refs.commit,
                    "findings": findings_count,
                    "actions": actions,
                },
                "link": _build_calendar_link(
                    "/pipelines",
                    {
                        "project": item.project_id,
                        "created_from": item.created.date().isoformat(),
                        "created_to": item.created.date().isoformat(),
                    },
                ),
            }
            return Response(event, status=status.HTTP_200_OK)

        if event_type == "pipeline_scheduled":
            schedule_token, _, ts_token = token.partition(":")
            try:
                schedule_id = int(schedule_token)
                run_ts = int(ts_token)
            except ValueError:
                return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)
            schedule = schedules.filter(id=schedule_id, enabled=True).first()
            if not schedule:
                return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)
            next_run = datetime.fromtimestamp(run_ts, tz=tzinfo)
            is_future = next_run > now_local
            event = {
                "id": f"pipeline_scheduled:{schedule.id}:{run_ts}",
                "event_type": "pipeline_scheduled",
                "title": f"Pipeline scheduled: {schedule.launch_config.project.product.name}",
                "start": next_run,
                "end": None,
                "is_all_day": False,
                "is_aggregated": False,
                "count": 1,
                "is_future": is_future,
                "color_variant": "future_single" if is_future else "past_single",
                "summary": {
                    "schedule_id": schedule.id,
                    "project_id": schedule.launch_config.project_id,
                    "project_name": schedule.launch_config.project.product.name,
                    "cron_expression": schedule.cron_expression,
                },
                "link": None,
            }
            return Response(event, status=status.HTTP_200_OK)

        if event_type in {"finding_created", "finding_mitigated"}:
            try:
                day = datetime.strptime(token, "%Y-%m-%d").date()
            except ValueError:
                day = None
            if day is None and event_type == "finding_created":
                try:
                    finding_id = int(token)
                except ValueError:
                    return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)
                finding = findings.filter(id=finding_id).first()
                if not finding:
                    return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)
                event_at = finding.date or finding.created
                started = timezone.localtime(event_at, tzinfo)
                is_future = started > now_local
                event = {
                    "id": f"finding_created:{finding.id}",
                    "event_type": "finding_created",
                    "title": f"Finding created: {finding.title}",
                    "start": started,
                    "end": None,
                    "is_all_day": False,
                    "is_aggregated": False,
                    "count": 1,
                    "is_future": is_future,
                    "color_variant": "future_single" if is_future else "past_single",
                    "summary": {"finding_id": finding.id, "severity": finding.severity},
                    "link": f"/finding/{finding.id}",
                }
                return Response(event, status=status.HTTP_200_OK)
            if day is None:
                return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)

            day_start = timezone.make_aware(datetime.combine(day, time.min), tzinfo)
            day_end = timezone.make_aware(datetime.combine(day + timedelta(days=1), time.min), tzinfo)

            if event_type == "finding_created":
                day_qs = findings.annotate(event_at=Coalesce("date", "created", output_field=DateTimeField())).filter(
                    event_at__gte=day_start,
                    event_at__lt=day_end,
                )
            else:
                day_qs = (
                    findings.filter(active=False)
                    .exclude(last_status_update__isnull=True)
                    .filter(last_status_update__gte=day_start, last_status_update__lt=day_end)
                )
            if not day_qs.exists():
                return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)

            severity = {"Critical": 0, "High": 0, "Medium": 0, "Low": 0, "Info": 0}
            for row in day_qs.values("severity").iterator():
                level = str(row.get("severity") or "")
                if level in severity:
                    severity[level] += 1
            total = int(day_qs.count())
            is_future = day_start > now_local
            if event_type == "finding_created":
                summary = {"severity": severity}
                title = f"Findings created: {total}"
            else:
                summary = {"severity": severity, "active": False}
                title = f"Findings mitigated: {total}"
            event = {
                "id": f"{event_type}:{day.isoformat()}",
                "event_type": event_type,
                "title": title,
                "start": day_start,
                "end": None,
                "is_all_day": True,
                "is_aggregated": True,
                "count": total,
                "is_future": is_future,
                "color_variant": "future_aggregate" if is_future else "past_aggregate",
                "summary": summary,
                "link": None,
            }
            return Response(event, status=status.HTTP_200_OK)

        return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)
