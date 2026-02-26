from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date as date_type
from datetime import datetime, time, timedelta
from typing import TYPE_CHECKING
from urllib.parse import urlencode

from croniter import croniter
from django.db.models import Count, DateTimeField
from django.db.models.functions import Coalesce
from django.urls import reverse
from django.utils import timezone
from dojo.authorization.roles_permissions import Permissions
from dojo.models import Finding
from drf_spectacular.utils import OpenApiParameter, OpenApiResponse, extend_schema
from rest_framework import serializers, status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from aist.api.calendar_domain import (
    CALENDAR_EVENT_TYPES,
    CalendarEventData,
    CalendarEventId,
    CalendarRequestContext,
    SeverityBucket,
    SeveritySummary,
    build_calendar_request_context,
)
from aist.api.common import CommaSeparatedListField, TimezoneNameField
from aist.api.schema import AISTApiTag
from aist.queries import (
    get_authorized_aist_launch_schedules,
    get_authorized_aist_pipelines,
    get_authorized_aist_projects,
    get_authorized_findings,
)
from aist.utils.project_version_refs import resolve_project_version_git_refs

if TYPE_CHECKING:
    from collections.abc import Callable


@dataclass(frozen=True, slots=True)
class CalendarApiChoices:
    view: list[str]
    grouping: list[str]
    event_type: list[str]


CALENDAR_API_CHOICES = CalendarApiChoices(
    view=["day", "week", "month"],
    grouping=["auto", "none"],
    event_type=list(CALENDAR_EVENT_TYPES),
)

MAX_RANGE_DAYS = 93
MAX_SCHEDULE_OCCURRENCES_PER_SCHEDULE = 300


class CalendarEventFactory:
    def __init__(self, *, tzinfo, now_local: datetime):
        self.tzinfo = tzinfo
        self.now_local = now_local

    def _to_local(self, value: datetime) -> datetime:
        return timezone.localtime(value, self.tzinfo)

    def _is_future(self, started: datetime) -> bool:
        return started > self.now_local

    @staticmethod
    def _single_variant(*, future: bool) -> str:
        return "future_single" if future else "past_single"

    @staticmethod
    def _aggregate_variant(*, future: bool) -> str:
        return "future_aggregate" if future else "past_aggregate"

    def _day_start(self, day: date_type) -> datetime:
        return timezone.make_aware(datetime.combine(day, time.min), self.tzinfo)

    @staticmethod
    def _findings_ui_link() -> str:
        return reverse("findings")

    @staticmethod
    def _finding_api_link(finding_id: int) -> str:
        return reverse("finding-detail", args=[finding_id])

    @staticmethod
    def _pipelines_ui_link(params: dict[str, str | int]) -> str:
        return f"{reverse('aist:pipeline_list')}?{urlencode(params)}"

    def project_created(self, project) -> CalendarEventData:
        started = self._to_local(project.created)
        is_future = self._is_future(started)
        return CalendarEventData(
            id=CalendarEventId.project_created(project.id).to_string(),
            event_type="project_created",
            title=f"Project created: {project.product.name}",
            start=started,
            end=None,
            is_all_day=False,
            is_aggregated=False,
            count=1,
            is_future=is_future,
            color_variant=self._single_variant(future=is_future),
            summary={"project_id": project.id, "project_name": project.product.name},
            link=self._findings_ui_link(),
        )

    def pipeline_started(self, pipeline, findings_count: int) -> CalendarEventData:
        started = self._to_local(pipeline.created)
        finished = self._to_local(pipeline.updated)
        is_future = self._is_future(started)
        refs = resolve_project_version_git_refs(pipeline.project_version)
        action_runs = (pipeline.launch_data or {}).get("action_runs") or []
        actions = [
            {
                "source": run.get("source"),
                "type": run.get("action_type"),
                "status": run.get("status"),
                "updated": run.get("updated_at"),
            }
            for run in action_runs
        ]
        duration_seconds = max(int((pipeline.updated - pipeline.created).total_seconds()), 0)
        link = self._pipelines_ui_link(
            {
                "project": pipeline.project_id,
                "created_from": pipeline.created.date().isoformat(),
                "created_to": pipeline.created.date().isoformat(),
            },
        )
        return CalendarEventData(
            id=CalendarEventId.pipeline_started(pipeline.id).to_string(),
            event_type="pipeline_started",
            title=f"Pipeline started: {pipeline.id}",
            start=started,
            end=finished if finished > started else None,
            is_all_day=False,
            is_aggregated=False,
            count=1,
            is_future=is_future,
            color_variant=self._single_variant(future=is_future),
            summary={
                "project_id": pipeline.project_id,
                "project_name": pipeline.project.product.name,
                "pipeline_id": pipeline.id,
                "status": pipeline.status,
                "created": pipeline.created.isoformat(),
                "updated": pipeline.updated.isoformat(),
                "duration_seconds": duration_seconds,
                "branch": refs.branch,
                "commit": refs.commit,
                "findings": findings_count,
                "actions": actions,
            },
            link=link,
        )

    def pipeline_scheduled(self, schedule, run_at: datetime, run_ts: int) -> CalendarEventData:
        is_future = self._is_future(run_at)
        return CalendarEventData(
            id=CalendarEventId.pipeline_scheduled(schedule.id, run_ts).to_string(),
            event_type="pipeline_scheduled",
            title=f"Pipeline scheduled: {schedule.launch_config.project.product.name}",
            start=run_at,
            end=None,
            is_all_day=False,
            is_aggregated=False,
            count=1,
            is_future=is_future,
            color_variant=self._single_variant(future=is_future),
            summary={
                "schedule_id": schedule.id,
                "project_id": schedule.launch_config.project_id,
                "project_name": schedule.launch_config.project.product.name,
                "cron_expression": schedule.cron_expression,
            },
            link=None,
        )

    def finding_created_single(self, finding, event_at: datetime) -> CalendarEventData:
        started = self._to_local(event_at)
        is_future = self._is_future(started)
        return CalendarEventData(
            id=CalendarEventId.finding_created(finding.id).to_string(),
            event_type="finding_created",
            title=f"Finding created: {finding.title}",
            start=started,
            end=None,
            is_all_day=False,
            is_aggregated=False,
            count=1,
            is_future=is_future,
            color_variant=self._single_variant(future=is_future),
            summary={"finding_id": finding.id, "severity": finding.severity},
            link=self._finding_api_link(finding.id),
        )

    def finding_created_aggregate(self, day: date_type, bucket: SeverityBucket) -> CalendarEventData:
        start_of_day = self._day_start(day)
        is_future = self._is_future(start_of_day)
        return CalendarEventData(
            id=CalendarEventId.finding_created(day.isoformat()).to_string(),
            event_type="finding_created",
            title=f"Findings created: {bucket.total}",
            start=start_of_day,
            end=None,
            is_all_day=True,
            is_aggregated=True,
            count=bucket.total,
            is_future=is_future,
            color_variant=self._aggregate_variant(future=is_future),
            summary={"severity": bucket.severity.to_dict()},
            link=None,
        )

    def finding_mitigated_aggregate(self, day: date_type, bucket: SeverityBucket) -> CalendarEventData:
        start_of_day = self._day_start(day)
        is_future = self._is_future(start_of_day)
        return CalendarEventData(
            id=CalendarEventId.finding_mitigated(day).to_string(),
            event_type="finding_mitigated",
            title=f"Findings mitigated: {bucket.total}",
            start=start_of_day,
            end=None,
            is_all_day=True,
            is_aggregated=True,
            count=bucket.total,
            is_future=is_future,
            color_variant=self._aggregate_variant(future=is_future),
            summary={"severity": bucket.severity.to_dict(), "active": False},
            link=None,
        )


class CalendarEventsRepository:
    def __init__(self, *, user, project_filter: set[int]):
        self.projects = get_authorized_aist_projects(Permissions.Product_View, user=user).select_related("product")
        self.pipelines = get_authorized_aist_pipelines(Permissions.Product_View, user=user).select_related(
            "project",
            "project__product",
        )
        self.schedules = get_authorized_aist_launch_schedules(Permissions.Product_View, user=user).select_related(
            "launch_config__project",
            "launch_config__project__product",
        )
        self.findings = get_authorized_findings(Permissions.Finding_View, user=user)

        if project_filter:
            self.projects = self.projects.filter(id__in=project_filter)
            self.pipelines = self.pipelines.filter(project_id__in=project_filter)
            self.schedules = self.schedules.filter(launch_config__project_id__in=project_filter)
            self.findings = self.findings.filter(aist_project_versions__project_id__in=project_filter).distinct()

    @staticmethod
    def findings_by_pipeline_ids(pipeline_ids: list[str]) -> dict[str, int]:
        if not pipeline_ids:
            return {}
        counts_qs = (
            Finding.objects.filter(test__aist_pipelines__id__in=pipeline_ids)
            .order_by()
            .values("test__aist_pipelines__id")
            .annotate(total=Count("id"))
        )
        return {
            str(row["test__aist_pipelines__id"]): int(row["total"])
            for row in counts_qs
        }

    @staticmethod
    def findings_count_for_pipeline(pipeline_id: str) -> int:
        return (
            Finding.objects.filter(test__aist_pipelines__id=pipeline_id)
            .order_by()
            .values("id")
            .distinct()
            .count()
        )


class BaseCalendarService:
    def __init__(self, *, context: CalendarRequestContext, repository: CalendarEventsRepository):
        self.ctx = context
        self.repo = repository
        self.factory = CalendarEventFactory(tzinfo=context.tzinfo, now_local=context.now_local)

    def _list_project_created(self) -> list[CalendarEventData]:
        rows = self.repo.projects.filter(created__gte=self.ctx.start, created__lt=self.ctx.end).order_by("created")
        return [self.factory.project_created(item) for item in rows]

    def _list_pipeline_started(self) -> list[CalendarEventData]:
        rows = list(self.repo.pipelines.filter(created__gte=self.ctx.start, created__lt=self.ctx.end).order_by("created"))
        pipeline_ids = [item.id for item in rows]
        findings_by_pipeline = CalendarEventsRepository.findings_by_pipeline_ids(pipeline_ids)
        return [self.factory.pipeline_started(item, findings_by_pipeline.get(item.id, 0)) for item in rows]

    def _list_pipeline_scheduled(self) -> list[CalendarEventData]:
        events: list[CalendarEventData] = []
        range_start_local = timezone.localtime(self.ctx.start, self.ctx.tzinfo)
        range_end_local = timezone.localtime(self.ctx.end, self.ctx.tzinfo)

        for schedule in self.repo.schedules.filter(enabled=True):
            occurrences = 0
            iterator = croniter(schedule.cron_expression, range_start_local)
            while occurrences < MAX_SCHEDULE_OCCURRENCES_PER_SCHEDULE:
                next_run = _ensure_aware(iterator.get_next(datetime), self.ctx.tzinfo)
                if next_run >= range_end_local:
                    break
                run_ts = int(next_run.timestamp())
                events.append(self.factory.pipeline_scheduled(schedule, next_run, run_ts))
                occurrences += 1
        return events

    def _list_finding_created(self) -> list[CalendarEventData]:
        findings_qs = (
            self.repo.findings.annotate(event_at=Coalesce("date", "created", output_field=DateTimeField()))
            .filter(event_at__gte=self.ctx.start, event_at__lt=self.ctx.end)
            .distinct()
        )
        if self.ctx.grouping == "none":
            rows = findings_qs.order_by("event_at", "id")[: self.ctx.limit]
            return [self.factory.finding_created_single(finding, finding.event_at) for finding in rows]

        by_day: dict[date_type, SeverityBucket] = defaultdict(SeverityBucket.empty)
        rows = findings_qs.values("severity", "event_at").order_by("event_at", "id")
        for row in rows:
            event_day = timezone.localtime(row["event_at"], self.ctx.tzinfo).date()
            bucket = by_day[event_day]
            bucket.total += 1
            bucket.severity.add(str(row.get("severity") or ""))

        return [self.factory.finding_created_aggregate(day, by_day[day]) for day in sorted(by_day)]

    def _list_finding_mitigated(self) -> list[CalendarEventData]:
        mitigated_qs = (
            self.repo.findings.filter(active=False)
            .exclude(last_status_update__isnull=True)
            .filter(last_status_update__gte=self.ctx.start, last_status_update__lt=self.ctx.end)
            .distinct()
        )
        by_day: dict[date_type, SeverityBucket] = defaultdict(SeverityBucket.empty)
        for row in mitigated_qs.values("severity", "last_status_update").order_by("last_status_update"):
            event_day = timezone.localtime(row["last_status_update"], self.ctx.tzinfo).date()
            bucket = by_day[event_day]
            bucket.total += 1
            bucket.severity.add(str(row.get("severity") or ""))

        return [self.factory.finding_mitigated_aggregate(day, by_day[day]) for day in sorted(by_day)]

    def _detail_project_created(self, token: str) -> CalendarEventData | None:
        try:
            project_id = int(token)
        except ValueError:
            return None
        project = self.repo.projects.filter(id=project_id).first()
        return self.factory.project_created(project) if project else None

    def _detail_pipeline_started(self, token: str) -> CalendarEventData | None:
        pipeline = self.repo.pipelines.filter(id=token).first()
        if not pipeline:
            return None
        findings_count = CalendarEventsRepository.findings_count_for_pipeline(pipeline.id)
        return self.factory.pipeline_started(pipeline, findings_count)

    def _detail_pipeline_scheduled(self, token: str) -> CalendarEventData | None:
        schedule_token, _, ts_token = token.partition(":")
        try:
            schedule_id = int(schedule_token)
            run_ts = int(ts_token)
        except ValueError:
            return None
        schedule = self.repo.schedules.filter(id=schedule_id, enabled=True).first()
        if not schedule:
            return None
        run_at = datetime.fromtimestamp(run_ts, tz=self.ctx.tzinfo)
        return self.factory.pipeline_scheduled(schedule, run_at, run_ts)

    def _detail_finding_created(self, token: str) -> CalendarEventData | None:
        day = _parse_day_token(token)
        if day is None:
            return self._detail_single_finding_created(token)
        return self._detail_finding_by_day(day=day, mitigated=False)

    def _detail_finding_mitigated(self, token: str) -> CalendarEventData | None:
        day = _parse_day_token(token)
        if day is None:
            return None
        return self._detail_finding_by_day(day=day, mitigated=True)

    def _detail_single_finding_created(self, token: str) -> CalendarEventData | None:
        try:
            finding_id = int(token)
        except ValueError:
            return None
        finding = self.repo.findings.filter(id=finding_id).first()
        if not finding:
            return None
        event_at = finding.date or finding.created
        return self.factory.finding_created_single(finding, event_at)

    def _detail_finding_by_day(self, *, day: date_type, mitigated: bool) -> CalendarEventData | None:
        day_start = timezone.make_aware(datetime.combine(day, time.min), self.ctx.tzinfo)
        day_end = timezone.make_aware(datetime.combine(day + timedelta(days=1), time.min), self.ctx.tzinfo)

        if mitigated:
            day_qs = (
                self.repo.findings.filter(active=False)
                .exclude(last_status_update__isnull=True)
                .filter(last_status_update__gte=day_start, last_status_update__lt=day_end)
            )
        else:
            day_qs = self.repo.findings.annotate(event_at=Coalesce("date", "created", output_field=DateTimeField())).filter(
                event_at__gte=day_start,
                event_at__lt=day_end,
            )

        if not day_qs.exists():
            return None

        severity = SeveritySummary.empty()
        for row in day_qs.values("severity").iterator():
            severity.add(str(row.get("severity") or ""))
        bucket = SeverityBucket(total=int(day_qs.count()), severity=severity)

        if mitigated:
            return self.factory.finding_mitigated_aggregate(day, bucket)
        return self.factory.finding_created_aggregate(day, bucket)


class CalendarEventListService(BaseCalendarService):
    def __init__(self, *, context: CalendarRequestContext, repository: CalendarEventsRepository):
        super().__init__(context=context, repository=repository)
        self.dispatch: dict[str, Callable[[], list[CalendarEventData]]] = {
            "project_created": self._list_project_created,
            "pipeline_started": self._list_pipeline_started,
            "pipeline_scheduled": self._list_pipeline_scheduled,
            "finding_created": self._list_finding_created,
            "finding_mitigated": self._list_finding_mitigated,
        }

    def list_events(self) -> list[CalendarEventData]:
        events: list[CalendarEventData] = []
        for event_type in self.ctx.event_types:
            events.extend(self.dispatch[event_type]())
        events.sort(key=lambda event: (event.start, event.id))
        return events


class CalendarEventDetailService(BaseCalendarService):
    def __init__(self, *, context: CalendarRequestContext, repository: CalendarEventsRepository):
        super().__init__(context=context, repository=repository)
        self.dispatch: dict[str, Callable[[str], CalendarEventData | None]] = {
            "project_created": self._detail_project_created,
            "pipeline_started": self._detail_pipeline_started,
            "pipeline_scheduled": self._detail_pipeline_scheduled,
            "finding_created": self._detail_finding_created,
            "finding_mitigated": self._detail_finding_mitigated,
        }

    def get_event_detail(self, event_id: CalendarEventId) -> CalendarEventData | None:
        handler = self.dispatch.get(event_id.event_type)
        if not handler:
            return None
        return handler(event_id.token)


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
    timezone = TimezoneNameField(required=False, allow_blank=True)
    grouping = serializers.ChoiceField(
        choices=CALENDAR_API_CHOICES.grouping,
        default="auto",
        required=False,
    )
    limit = serializers.IntegerField(default=500, min_value=1, max_value=2000, required=False)
    event_types = CommaSeparatedListField(
        child=serializers.ChoiceField(choices=CALENDAR_API_CHOICES.event_type),
        required=False,
    )
    project_id = CommaSeparatedListField(
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
        attrs["event_types"] = tuple(dict.fromkeys(attrs.get("event_types", CALENDAR_API_CHOICES.event_type)))
        attrs["project_id"] = list(dict.fromkeys(attrs.get("project_id", [])))
        return attrs


class CalendarEventDetailQuerySerializer(serializers.Serializer):
    timezone = TimezoneNameField(required=False, allow_blank=True)
    project_id = CommaSeparatedListField(
        child=serializers.IntegerField(min_value=1),
        required=False,
    )

    def validate(self, attrs):
        attrs["project_id"] = list(dict.fromkeys(attrs.get("project_id", [])))
        return attrs


class AISTCalendarEventsAPI(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(
        tags=[AISTApiTag.CALENDAR.value],
        operation_id="aist_calendar_events_list",
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
        params = CalendarEventsQuerySerializer(data=request.query_params)
        if not params.is_valid():
            return Response(params.errors, status=status.HTTP_400_BAD_REQUEST)

        context = build_calendar_request_context(params.validated_data)
        repository = CalendarEventsRepository(user=request.user, project_filter=context.project_ids)
        service = CalendarEventListService(context=context, repository=repository)
        events = service.list_events()

        truncated = len(events) > context.limit
        sliced_events = events[: context.limit]

        return Response(
            {
                "range": {
                    "start": context.start.isoformat(),
                    "end": context.end.isoformat(),
                    "timezone": str(context.tzinfo),
                    "view": context.view,
                },
                "events": [event.to_dict() for event in sliced_events],
                "meta": {"total": len(sliced_events), "truncated": truncated},
            },
            status=status.HTTP_200_OK,
        )


class AISTCalendarEventDetailAPI(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(
        tags=[AISTApiTag.CALENDAR.value],
        operation_id="aist_calendar_event_detail_retrieve",
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
        params = CalendarEventDetailQuerySerializer(data=request.query_params)
        if not params.is_valid():
            return Response(params.errors, status=status.HTTP_400_BAD_REQUEST)

        parsed_event = CalendarEventId.parse(event_id)
        if not parsed_event:
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)

        context = build_calendar_request_context(
            {
                "start": timezone.now(),
                "end": timezone.now() + timedelta(days=1),
                "view": "day",
                "grouping": "auto",
                "limit": 1,
                "event_types": [parsed_event.event_type],
                "project_id": params.validated_data.get("project_id", []),
                "timezone": params.validated_data.get("timezone", ""),
            },
        )
        repository = CalendarEventsRepository(user=request.user, project_filter=context.project_ids)
        service = CalendarEventDetailService(context=context, repository=repository)
        event = service.get_event_detail(parsed_event)
        if not event:
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)
        return Response(event.to_dict(), status=status.HTTP_200_OK)


def _ensure_aware(value: datetime, tzinfo) -> datetime:
    if timezone.is_naive(value):
        return timezone.make_aware(value, tzinfo)
    return value


def _parse_day_token(token: str) -> date_type | None:
    try:
        return datetime.strptime(token, "%Y-%m-%d").date()
    except ValueError:
        return None
