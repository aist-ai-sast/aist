from __future__ import annotations

import json
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

from django import template
from django.utils import timezone
from django.utils.timesince import timesince

from aist.models import AISTStatus
from aist.services.pipeline_lifecycle import is_terminal_pipeline_status, pipeline_elapsed

if TYPE_CHECKING:
    from datetime import timedelta

register = template.Library()

_STATUS_BADGE_CLASSES = {
    AISTStatus.FINISHED: "bg-green-600",
    AISTStatus.FINISHED_WITH_WARNINGS: "bg-amber-500 text-dark",
}
_UNFINISHED_BADGE_CLASS = "bg-gray-500"


class PanelHydrator(StrEnum):

    """The page script that runs a status panel; pipeline_detail.html keys its HYDRATORS by these values."""

    FINISHED = "finished"
    WAITING_DEDUP = "waiting-dedup"
    POSTPROCESSING = "postprocessing"
    PUSH_TO_AI = "push-to-ai"


@dataclass(frozen=True, slots=True)
class StatusPanel:

    """What the pipeline page shows for a status: a partial (none when blank) and the page script that runs it."""

    template: str = ""
    hydrator: PanelHydrator | None = None


_FINISHED_PANEL = StatusPanel("aist/_status_finished.html", PanelHydrator.FINISHED)
_STATUS_PANELS = {
    AISTStatus.ADMITTED: StatusPanel("aist/_status_admitted.html"),
    AISTStatus.EXECUTING: StatusPanel("aist/_status_executing.html"),
    AISTStatus.UPLOADING_RESULTS: StatusPanel(),
    AISTStatus.FINDING_POSTPROCESSING: StatusPanel("aist/_status_finding_postprocessing.html", PanelHydrator.POSTPROCESSING),
    AISTStatus.WAITING_DEDUPLICATION_TO_FINISH: StatusPanel("aist/_status_waiting_dedup.html", PanelHydrator.WAITING_DEDUP),
    AISTStatus.WAITING_CONFIRMATION_TO_PUSH_TO_AI: StatusPanel("aist/_confirm_push_to_ai.html", PanelHydrator.PUSH_TO_AI),
    AISTStatus.PUSH_TO_AI: StatusPanel(),
    AISTStatus.WAITING_RESULT_FROM_AI: StatusPanel("aist/_status_waiting_ai.html"),
    AISTStatus.FINISHED: _FINISHED_PANEL,
    AISTStatus.FINISHED_WITH_WARNINGS: _FINISHED_PANEL,
}
_UNKNOWN_STATUS_PANEL = StatusPanel("aist/_status_unknown.html")


@register.filter
def to_pretty_json(value):
    try:
        return json.dumps(value, ensure_ascii=False, indent=2)
    except Exception:
        return "(unserializable)"


@register.filter
def versions_json(queryset):
    """Serialize a project versions queryset to a JSON array for use in data-* HTML attributes."""
    try:
        data = [
            {"id": str(v.id), "label": str(v), "version": v.version, "type": str(v.version_type)}
            for v in queryset
        ]
        return json.dumps(data, ensure_ascii=True)
    except Exception:
        return "[]"


def _format_elapsed(elapsed: timedelta) -> str:
    """``HH:MM:SS``; hours grow past 99 rather than roll over into days. ``page-behaviours.js`` counts in this form."""
    hours, rem = divmod(int(elapsed.total_seconds()), 3600)
    minutes, seconds = divmod(rem, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


@register.inclusion_tag("aist/_pipeline_duration.html")
def pipeline_duration(pipeline):
    """A pipeline's run time as ``HH:MM:SS``, counting up on the page while the pipeline is not finished."""
    elapsed = pipeline_elapsed(pipeline, timezone.now())
    return {
        "seconds": int(elapsed.total_seconds()),
        "text": _format_elapsed(elapsed),
        "live": not is_terminal_pipeline_status(pipeline.status),
    }


@register.simple_tag
def pipeline_duration_words(pipeline) -> str:
    """A pipeline's run time in words, its two largest units, e.g. "1 hour, 59 minutes"."""
    elapsed = pipeline_elapsed(pipeline, timezone.now())
    return timesince(pipeline.created, pipeline.created + elapsed)


@register.inclusion_tag("aist/_pipeline_status_badge.html")
def pipeline_status_badge(pipeline):
    """The pipeline's status as a badge coloured by outcome; every unfinished status shares one neutral colour."""
    return {
        "status": pipeline.status,
        "css_class": _STATUS_BADGE_CLASSES.get(pipeline.status, _UNFINISHED_BADGE_CLASS),
    }


@register.simple_tag
def pipeline_status_panel(pipeline) -> StatusPanel:
    return _STATUS_PANELS.get(pipeline.status, _UNKNOWN_STATUS_PANEL)


@register.filter
def pipeline_is_active(pipeline) -> bool:
    """Whether the pipeline is not yet in a terminal status."""
    return not is_terminal_pipeline_status(pipeline.status)
