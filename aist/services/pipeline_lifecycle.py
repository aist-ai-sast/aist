from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from django.db import transaction
from django.utils import timezone

from aist.models import AISTPipeline, AISTStatus, PipelineExecutionLease
from aist.signals import pipeline_status_changed

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

TERMINAL_PIPELINE_STATUSES = frozenset({
    AISTStatus.FINISHED,
    AISTStatus.FINISHED_WITH_WARNINGS,
})
SUCCESSFUL_PIPELINE_STATUSES = TERMINAL_PIPELINE_STATUSES
ACTIVE_PIPELINE_STATUSES = frozenset({
    AISTStatus.ADMITTED,
    AISTStatus.EXECUTING,
    AISTStatus.UPLOADING_RESULTS,
    AISTStatus.FINDING_POSTPROCESSING,
    AISTStatus.WAITING_DEDUPLICATION_TO_FINISH,
    AISTStatus.WAITING_CONFIRMATION_TO_PUSH_TO_AI,
    AISTStatus.PUSH_TO_AI,
    AISTStatus.WAITING_RESULT_FROM_AI,
})

_TERMINAL = set(TERMINAL_PIPELINE_STATUSES)
PIPELINE_TRANSITIONS: Mapping[str, frozenset[str]] = {
    AISTStatus.ADMITTED: frozenset({AISTStatus.EXECUTING, *_TERMINAL}),
    AISTStatus.EXECUTING: frozenset({
        AISTStatus.UPLOADING_RESULTS,
        *_TERMINAL,
    }),
    AISTStatus.UPLOADING_RESULTS: frozenset({
        AISTStatus.WAITING_DEDUPLICATION_TO_FINISH,
        AISTStatus.FINDING_POSTPROCESSING,
        *_TERMINAL,
    }),
    AISTStatus.WAITING_DEDUPLICATION_TO_FINISH: frozenset({
        AISTStatus.FINDING_POSTPROCESSING,
        *_TERMINAL,
    }),
    AISTStatus.FINDING_POSTPROCESSING: frozenset({
        AISTStatus.WAITING_CONFIRMATION_TO_PUSH_TO_AI,
        AISTStatus.PUSH_TO_AI,
        *_TERMINAL,
    }),
    AISTStatus.WAITING_CONFIRMATION_TO_PUSH_TO_AI: frozenset({
        AISTStatus.PUSH_TO_AI,
        *_TERMINAL,
    }),
    AISTStatus.PUSH_TO_AI: frozenset({
        AISTStatus.WAITING_RESULT_FROM_AI,
        *_TERMINAL,
    }),
    AISTStatus.WAITING_RESULT_FROM_AI: frozenset({
        AISTStatus.PUSH_TO_AI,
        *_TERMINAL,
    }),
    AISTStatus.FINISHED: frozenset(),
    AISTStatus.FINISHED_WITH_WARNINGS: frozenset(),
}


class PipelineTransitionError(ValueError):

    """Raised when a caller attempts to bypass the pipeline lifecycle graph."""


@dataclass(frozen=True, slots=True)
class PipelineTransitionResult:
    pipeline: AISTPipeline
    changed: bool
    old_status: str
    new_status: str


def is_terminal_pipeline_status(status: str) -> bool:
    return status in TERMINAL_PIPELINE_STATUSES


def transition_pipeline_status(
    pipeline_id: str,
    new_status: str,
    *,
    field_updates: Mapping[str, object] | None = None,
    update_fields: Sequence[str] | None = None,
) -> PipelineTransitionResult:
    """
    Apply one idempotent, row-locked lifecycle transition.

    Status, lifecycle timestamps, task ownership, lease release and the emitted
    transition event are committed as one unit. ``field_updates`` is intentionally
    explicit so callers cannot save a stale pipeline object around the transition.
    """
    if new_status not in PIPELINE_TRANSITIONS:
        detail = f"Unknown pipeline status: {new_status}."
        raise PipelineTransitionError(detail)

    with transaction.atomic():
        pipeline = AISTPipeline.objects.select_for_update().get(pk=pipeline_id)
        old_status = pipeline.status
        if old_status == new_status:
            return PipelineTransitionResult(
                pipeline=pipeline,
                changed=False,
                old_status=old_status,
                new_status=new_status,
            )
        if new_status not in PIPELINE_TRANSITIONS.get(old_status, frozenset()):
            detail = f"Pipeline status transition {old_status} -> {new_status} is not allowed."
            raise PipelineTransitionError(detail)

        changed_fields = {"status", "updated"}
        for field_name in update_fields or ():
            if field_updates is None or field_name not in field_updates:
                detail = f"Missing value for pipeline field update: {field_name}."
                raise PipelineTransitionError(detail)
            setattr(pipeline, field_name, field_updates[field_name])
            changed_fields.add(field_name)

        now = timezone.now()
        pipeline.status = new_status
        if new_status == AISTStatus.EXECUTING and pipeline.started is None:
            pipeline.started = now
            changed_fields.add("started")
        if is_terminal_pipeline_status(new_status):
            if pipeline.finished_at is None:
                pipeline.finished_at = now
                changed_fields.add("finished_at")
            pipeline.run_task_id = None
            changed_fields.add("run_task_id")
            PipelineExecutionLease.objects.filter(
                pipeline_id=pipeline.pk,
                released_at__isnull=True,
            ).update(released_at=now)

        pipeline.save(update_fields=sorted(changed_fields))
        transaction.on_commit(lambda: pipeline_status_changed.send(
            sender=AISTPipeline,
            pipeline_id=pipeline.pk,
            old_status=old_status,
            new_status=new_status,
        ))
        return PipelineTransitionResult(
            pipeline=pipeline,
            changed=True,
            old_status=old_status,
            new_status=new_status,
        )
