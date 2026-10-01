"""
Work item state of a finding: one value per finding.

A finding can be linked to several tickets (Jira and GitLab on the same
finding). Every place that groups or filters findings by work item status uses
this one definition, so a dashboard segment and the Findings list behind it
always hold the same findings.
"""
from __future__ import annotations

from django.db.models import Case, IntegerField, OuterRef, QuerySet, Subquery, Value, When

from aist.models import WorkItemLink, WorkItemStatusCategory

# The ticket that still has work left decides: a finding is Done only when
# none of its tickets is open or in progress.
STATE_PRECEDENCE = (
    WorkItemStatusCategory.IN_PROGRESS,
    WorkItemStatusCategory.OPEN,
    WorkItemStatusCategory.UNKNOWN,
    WorkItemStatusCategory.DONE,
    WorkItemStatusCategory.CANCELLED,
)

# Filter value and dashboard state of a finding without any ticket.
NO_WORK_ITEM = "none"

STATE_FIELD = "work_item_state"
LINKED_AT_FIELD = "work_item_linked_at"


def _links_of_outer_finding() -> QuerySet:
    return WorkItemLink.objects.filter(finding=OuterRef("pk"))


def with_work_item_state(findings: QuerySet) -> QuerySet:
    """Annotate ``work_item_state``: the deciding ticket's status category, NULL without tickets."""
    if STATE_FIELD in findings.query.annotations:
        return findings
    rank = Case(
        *(When(status_category=category, then=Value(index)) for index, category in enumerate(STATE_PRECEDENCE)),
        default=Value(len(STATE_PRECEDENCE)),
        output_field=IntegerField(),
    )
    deciding = _links_of_outer_finding().annotate(_rank=rank).order_by("_rank", "id").values("status_category")[:1]
    return findings.annotate(**{STATE_FIELD: Subquery(deciding)})


def with_work_item_linked_at(findings: QuerySet) -> QuerySet:
    """Annotate ``work_item_linked_at``: when the finding got its first ticket, NULL without tickets."""
    if LINKED_AT_FIELD in findings.query.annotations:
        return findings
    first = _links_of_outer_finding().order_by("created", "id").values("created")[:1]
    return findings.annotate(**{LINKED_AT_FIELD: Subquery(first)})
