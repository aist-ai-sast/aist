"""
Narrow, index-friendly queries against dojo_findingevent (pghistory snapshot table).

Replaces the heavy DojoEvents CTE/UNION for Finding-specific history lookups.
The table stores full-row snapshots; to detect field changes we compare consecutive
snapshots with a correlated subquery on (pgh_obj_id, pgh_id).

Key indices relied upon:
  - PRIMARY KEY on pgh_id
  - FK index on pgh_obj_id (created by Django for ForeignKey)
  - AIST migration 0017 adds (pgh_obj_id, pgh_id DESC) composite index for the
    correlated subquery used in severity_changed_events().
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from django.apps import apps
from django.db.models import F, OuterRef, Q, Subquery
from pghistory.models import Context

if TYPE_CHECKING:
    from datetime import datetime

    from django.db.models import QuerySet


def severity_changed_events(finding_ids: QuerySet, start: datetime, end: datetime) -> QuerySet:
    """
    Return FindingEvent rows (pgh_label='update') where severity changed, within [start, end).

    Uses a correlated subquery to fetch the immediately preceding snapshot for the
    same finding and compares severity values.  The outer filter is applied first
    (date range + finding scope), so only candidate rows execute the subquery.

    finding_ids: a values("id") QuerySet of authorized Finding PKs.
    """
    FindingEvent = apps.get_model("dojo", "FindingEvent")

    # Correlated subquery: previous snapshot for the same finding (by pgh_id ordering)
    prev_severity_sq = (
        FindingEvent.objects.filter(
            pgh_obj_id=OuterRef("pgh_obj_id"),
            pgh_id__lt=OuterRef("pgh_id"),
        )
        .order_by("-pgh_id")
        .values("severity")[:1]
    )

    return (
        FindingEvent.objects.filter(
            pgh_label="update",
            pgh_obj_id__in=finding_ids,
            pgh_created_at__gte=start,
            pgh_created_at__lt=end,
        )
        .annotate(prev_severity=Subquery(prev_severity_sq))
        .exclude(Q(prev_severity__isnull=True) | Q(prev_severity=F("severity")))
        .order_by("-pgh_created_at", "-pgh_id")
    )


def history_events_with_users(
    finding_ids: QuerySet,
    start: datetime,
    end: datetime,
) -> list[dict]:
    """
    Return all insert/update events for the given findings within [start, end),
    augmented with a 'user' field from the pghistory context.

    Used by the timeline owner-index builder.  Returns a list of plain dicts:
      pgh_obj_id (int), pgh_label (str), pgh_created_at (datetime),
      last_status_update (datetime|None), user (int|None)
    """
    FindingEvent = apps.get_model("dojo", "FindingEvent")

    event_rows = list(
        FindingEvent.objects.filter(
            pgh_obj_id__in=finding_ids,
            pgh_created_at__gte=start,
            pgh_created_at__lt=end,
        )
        .values("pgh_obj_id", "pgh_label", "pgh_created_at", "pgh_context_id", "last_status_update")
        .order_by("-pgh_created_at", "-pgh_id"),
    )

    if not event_rows:
        return []

    context_ids = {row["pgh_context_id"] for row in event_rows if row.get("pgh_context_id")}
    context_user_map: dict = {}
    if context_ids:
        context_user_map = {
            ctx["id"]: ctx["metadata"].get("user")
            for ctx in Context.objects.filter(id__in=context_ids).values("id", "metadata")
            if ctx.get("metadata")
        }

    for row in event_rows:
        row["user"] = context_user_map.get(row.get("pgh_context_id"))

    return event_rows
