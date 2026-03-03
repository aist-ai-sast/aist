from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from django.utils import timezone

from aist.api.common import empty_severity_counts

if TYPE_CHECKING:
    from datetime import date as date_type
    from datetime import datetime

CALENDAR_EVENT_TYPES: tuple[str, ...] = (
    "pipeline_started",
    "pipeline_scheduled",
    "finding_created",
    "finding_processed",
    "project_created",
)


@dataclass(frozen=True, slots=True)
class CalendarRequestContext:
    start: datetime
    end: datetime
    view: str
    grouping: str
    limit: int
    event_types: tuple[str, ...]
    project_ids: set[int]
    tzinfo: object
    now_local: datetime


def build_calendar_request_context(validated_data: dict[str, object]) -> CalendarRequestContext:
    tz_name = str(validated_data.get("timezone") or "").strip()
    tzinfo = ZoneInfo(tz_name) if tz_name else timezone.get_current_timezone()
    return CalendarRequestContext(
        start=validated_data["start"],
        end=validated_data["end"],
        view=validated_data["view"],
        grouping=validated_data["grouping"],
        limit=validated_data["limit"],
        event_types=tuple(validated_data["event_types"]),
        project_ids=set(validated_data.get("project_id", [])),
        tzinfo=tzinfo,
        now_local=timezone.localtime(timezone.now(), tzinfo),
    )


@dataclass(frozen=True, slots=True)
class CalendarEventData:
    id: str
    event_type: str
    title: str
    start: datetime
    end: datetime | None
    is_all_day: bool
    is_aggregated: bool
    count: int
    is_future: bool
    color_variant: str
    summary: dict[str, object]
    link: str | None

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "event_type": self.event_type,
            "title": self.title,
            "start": self.start,
            "end": self.end,
            "is_all_day": self.is_all_day,
            "is_aggregated": self.is_aggregated,
            "count": self.count,
            "is_future": self.is_future,
            "color_variant": self.color_variant,
            "summary": self.summary,
            "link": self.link,
        }


@dataclass(frozen=True, slots=True)
class CalendarEventId:
    event_type: str
    token: str

    @classmethod
    def parse(cls, event_id: str) -> CalendarEventId | None:
        event_type, _, token = event_id.partition(":")
        if not token or event_type not in CALENDAR_EVENT_TYPES:
            return None
        return cls(event_type=event_type, token=token)

    @classmethod
    def project_created(cls, project_id: int) -> CalendarEventId:
        return cls(event_type="project_created", token=str(project_id))

    @classmethod
    def pipeline_started(cls, pipeline_id: str) -> CalendarEventId:
        return cls(event_type="pipeline_started", token=str(pipeline_id))

    @classmethod
    def pipeline_scheduled(cls, schedule_id: int, run_ts: int) -> CalendarEventId:
        return cls(event_type="pipeline_scheduled", token=f"{schedule_id}:{run_ts}")

    @classmethod
    def finding_created(cls, token: int | str) -> CalendarEventId:
        return cls(event_type="finding_created", token=str(token))

    @classmethod
    def finding_processed(cls, day: date_type) -> CalendarEventId:
        return cls(event_type="finding_processed", token=day.isoformat())

    def to_string(self) -> str:
        return f"{self.event_type}:{self.token}"


@dataclass(slots=True)
class SeveritySummary:
    counts: dict[str, int]

    @classmethod
    def empty(cls) -> SeveritySummary:
        return cls(counts=empty_severity_counts())

    def add(self, level: str | None) -> None:
        if level in self.counts:
            self.counts[level] += 1

    def to_dict(self) -> dict[str, int]:
        return dict(self.counts)


@dataclass(slots=True)
class SeverityBucket:
    total: int
    severity: SeveritySummary

    @classmethod
    def empty(cls) -> SeverityBucket:
        return cls(total=0, severity=SeveritySummary.empty())
