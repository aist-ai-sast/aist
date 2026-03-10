from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import TYPE_CHECKING

from django.db.models import DateTimeField, QuerySet
from django.db.models.functions import Coalesce
from django.utils import timezone

from aist.api.calendar_domain import SeverityBucket
from aist.api.finding_history import severity_changed_events

if TYPE_CHECKING:
    from datetime import date as date_type
    from datetime import datetime

PROCESSED_REASONS: tuple[str, ...] = (
    "mitigated",
    "resolved",
    "false_positive",
    "out_of_scope",
    "duplicate",
    "severity_changed",
)


@dataclass(frozen=True, slots=True)
class FindingTimelineRow:
    event_type: str
    finding_id: int
    title: str
    severity: str
    happened_at: datetime
    project_ids: list[int]
    processed_reason: str | None = None


@dataclass(slots=True)
class ProcessedBucket:
    total: int
    severity: SeverityBucket
    reasons: dict[str, int]

    @classmethod
    def empty(cls) -> ProcessedBucket:
        return cls(
            total=0,
            severity=SeverityBucket.empty(),
            reasons=dict.fromkeys(PROCESSED_REASONS, 0),
        )

    def add(self, *, severity: str | None, reason: str) -> None:
        self.total += 1
        self.severity.total += 1
        self.severity.severity.add(severity or "")
        self.reasons[reason] = int(self.reasons.get(reason, 0)) + 1


class FindingEventStream:
    def __init__(self, *, findings: QuerySet, tzinfo):
        self.findings = findings
        self.tzinfo = tzinfo

    def list_created(self, *, start: datetime, end: datetime) -> QuerySet:
        return (
            self.findings.annotate(event_at=Coalesce("date", "created", output_field=DateTimeField()))
            .filter(event_at__gte=start, event_at__lt=end)
            .distinct()
        )

    def list_status_processed(self, *, start: datetime, end: datetime) -> QuerySet:
        return (
            self.findings.filter(active=False)
            .exclude(last_status_update__isnull=True)
            .filter(last_status_update__gte=start, last_status_update__lt=end)
            .distinct()
        )

    def list_severity_changed_events(self, *, start: datetime, end: datetime) -> QuerySet:
        """
        Return FindingEvent rows where severity changed within [start, end),
        scoped to authorized findings.  Each row exposes direct model fields
        (severity, title, pgh_obj_id as int, pgh_created_at).
        """
        finding_ids = self.findings.values("id")
        return severity_changed_events(finding_ids, start, end)

    def aggregate_created_by_day(self, *, start: datetime, end: datetime) -> dict[date_type, SeverityBucket]:
        return self._aggregate_by_day(
            rows=self.list_created(start=start, end=end).values("id", "severity", "event_at").order_by("event_at"),
            timestamp_field="event_at",
        )

    def aggregate_processed_by_day(self, *, start: datetime, end: datetime) -> dict[date_type, ProcessedBucket]:
        by_day: dict[date_type, ProcessedBucket] = defaultdict(ProcessedBucket.empty)
        for row in (
            self.list_status_processed(start=start, end=end)
            .values("id", "severity", "is_mitigated", "false_p", "out_of_scope", "duplicate", "last_status_update")
            .order_by("last_status_update")
        ):
            event_at = row.get("last_status_update")
            if not event_at:
                continue
            reason = self._status_processed_reason(row)
            event_day = timezone.localtime(event_at, self.tzinfo).date()
            by_day[event_day].add(severity=str(row.get("severity") or ""), reason=reason)

        # severity_changed_events rows expose direct fields (no pgh_data wrapping)
        for row in self.list_severity_changed_events(start=start, end=end).values("severity", "pgh_created_at"):
            event_at = row.get("pgh_created_at")
            if not event_at:
                continue
            event_day = timezone.localtime(event_at, self.tzinfo).date()
            by_day[event_day].add(severity=str(row.get("severity") or ""), reason="severity_changed")
        return by_day

    def aggregate_created_for_range(self, *, start: datetime, end: datetime) -> SeverityBucket | None:
        return self._aggregate_for_range(rows=self.list_created(start=start, end=end).values("id", "severity"))

    def aggregate_processed_for_range(self, *, start: datetime, end: datetime) -> ProcessedBucket | None:
        processed = ProcessedBucket.empty()
        has_values = False
        for row in self.list_status_processed(start=start, end=end).values(
            "id",
            "severity",
            "is_mitigated",
            "false_p",
            "out_of_scope",
            "duplicate",
        ):
            processed.add(
                severity=str(row.get("severity") or ""),
                reason=self._status_processed_reason(row),
            )
            has_values = True
        for row in self.list_severity_changed_events(start=start, end=end).values("severity"):
            processed.add(severity=str(row.get("severity") or ""), reason="severity_changed")
            has_values = True
        return processed if has_values else None

    def processed_finding_ids(self, *, start: datetime, end: datetime) -> set[int]:
        ids = set(
            self.list_status_processed(start=start, end=end)
            .values_list("id", flat=True),
        )
        # pgh_obj_id is an integer FK on FindingEvent (not text as in DojoEvents)
        severity_event_ids = self.list_severity_changed_events(start=start, end=end).values_list(
            "pgh_obj_id", flat=True,
        )
        ids.update(item for item in severity_event_ids if item is not None)
        return ids

    def timeline_rows(self, *, start: datetime, end: datetime, limit: int) -> list[FindingTimelineRow]:
        created_rows = (
            self.list_created(start=start, end=end)
            .values("id", "title", "severity", "event_at", "aist_project_versions__project_id")
            .order_by("-event_at", "-id")[:limit]
        )
        processed_rows = (
            self.list_status_processed(start=start, end=end)
            .values(
                "id",
                "title",
                "severity",
                "last_status_update",
                "is_mitigated",
                "false_p",
                "out_of_scope",
                "duplicate",
                "aist_project_versions__project_id",
            )
            .order_by("-last_status_update", "-id")[:limit]
        )
        timeline: list[FindingTimelineRow] = []
        timeline.extend(self._timeline_rows_from_values(created_rows, "finding_created", "event_at"))
        timeline.extend(
            self._timeline_rows_from_values(
                processed_rows,
                "finding_processed",
                "last_status_update",
                processed_reason_fn=self._status_processed_reason,
            ),
        )
        timeline.extend(self._timeline_rows_from_severity_events(start=start, end=end, limit=limit))
        timeline.sort(key=lambda row: (row.happened_at, row.finding_id, row.event_type), reverse=True)
        return timeline[:limit]

    def _timeline_rows_from_severity_events(
        self,
        *,
        start: datetime,
        end: datetime,
        limit: int,
    ) -> list[FindingTimelineRow]:
        # FindingEvent rows expose direct fields; pgh_obj_id is an int FK
        rows = self.list_severity_changed_events(start=start, end=end).values(
            "pgh_obj_id",
            "severity",
            "title",
            "pgh_created_at",
        )[:limit]
        finding_ids = {row["pgh_obj_id"] for row in rows if row.get("pgh_obj_id") is not None}
        project_map = self._project_ids_by_finding(finding_ids)
        timeline: list[FindingTimelineRow] = []
        for row in rows:
            finding_id = row.get("pgh_obj_id")
            happened_at = row.get("pgh_created_at")
            if finding_id is None or not happened_at:
                continue
            title = str(row.get("title") or f"Finding {finding_id}")
            severity = str(row.get("severity") or "")
            timeline.append(
                FindingTimelineRow(
                    event_type="finding_processed",
                    finding_id=finding_id,
                    title=title,
                    severity=severity,
                    happened_at=happened_at,
                    project_ids=project_map.get(finding_id, []),
                    processed_reason="severity_changed",
                ),
            )
        return timeline

    def _project_ids_by_finding(self, finding_ids: set[int]) -> dict[int, list[int]]:
        if not finding_ids:
            return {}
        project_map: dict[int, set[int]] = defaultdict(set)
        rows = (
            self.findings.filter(id__in=finding_ids)
            .values("id", "aist_project_versions__project_id")
            .order_by("id")
        )
        for row in rows:
            finding_id = int(row["id"])
            project_id = row.get("aist_project_versions__project_id")
            if project_id:
                project_map[finding_id].add(int(project_id))
        return {finding_id: sorted(projects) for finding_id, projects in project_map.items()}

    def _timeline_rows_from_values(
        self,
        rows,
        event_type: str,
        timestamp_field: str,
        processed_reason_fn=None,
    ) -> list[FindingTimelineRow]:
        grouped: dict[tuple[int, datetime, str | None], FindingTimelineRow] = {}
        for row in rows:
            happened_at = row.get(timestamp_field)
            if not happened_at:
                continue
            finding_id = int(row["id"])
            processed_reason = processed_reason_fn(row) if processed_reason_fn else None
            key = (finding_id, happened_at, processed_reason)
            existing = grouped.get(key)
            project_id = row.get("aist_project_versions__project_id")
            if existing:
                project_ids = set(existing.project_ids)
                if project_id:
                    project_ids.add(int(project_id))
                grouped[key] = FindingTimelineRow(
                    event_type=existing.event_type,
                    finding_id=existing.finding_id,
                    title=existing.title,
                    severity=existing.severity,
                    happened_at=existing.happened_at,
                    project_ids=sorted(project_ids),
                    processed_reason=existing.processed_reason,
                )
                continue
            grouped[key] = FindingTimelineRow(
                event_type=event_type,
                finding_id=finding_id,
                title=str(row.get("title") or f"Finding {finding_id}"),
                severity=str(row.get("severity") or ""),
                happened_at=happened_at,
                project_ids=[int(project_id)] if project_id else [],
                processed_reason=processed_reason,
            )
        return list(grouped.values())

    def _aggregate_by_day(self, *, rows, timestamp_field: str) -> dict[date_type, SeverityBucket]:
        by_day: dict[date_type, SeverityBucket] = defaultdict(SeverityBucket.empty)
        for row in rows:
            event_at = row.get(timestamp_field)
            if not event_at:
                continue
            event_day = timezone.localtime(event_at, self.tzinfo).date()
            bucket = by_day[event_day]
            bucket.total += 1
            bucket.severity.add(str(row.get("severity") or ""))
        return by_day

    @staticmethod
    def _aggregate_for_range(*, rows) -> SeverityBucket | None:
        severity_rows = list(rows)
        if not severity_rows:
            return None
        bucket = SeverityBucket.empty()
        for row in severity_rows:
            bucket.total += 1
            bucket.severity.add(str(row.get("severity") or ""))
        return bucket

    @staticmethod
    def _status_processed_reason(row: dict[str, object]) -> str:
        if bool(row.get("is_mitigated")):
            return "mitigated"
        if bool(row.get("false_p")):
            return "false_positive"
        if bool(row.get("out_of_scope")):
            return "out_of_scope"
        if bool(row.get("duplicate")):
            return "duplicate"
        return "resolved"
