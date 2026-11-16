from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING

from django.db import transaction
from django.utils import timezone

from aist.models import AISTPipeline, AISTStatus, LocalTriageBatch, LocalTriageBatchState, PipelineExecutionLease
from aist.signals import pipeline_status_changed

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from datetime import datetime

    from django.db.models import QuerySet

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

# Local triage batches that still read the pipeline's sources.
OPEN_LOCAL_TRIAGE_BATCH_STATES = (
    LocalTriageBatchState.PENDING,
    LocalTriageBatchState.RUNNING,
    LocalTriageBatchState.CANCELLING,
)

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

    """Raised for an unknown status, a move the lifecycle graph forbids, or a declared field update with no value."""


@dataclass(frozen=True, slots=True)
class PipelineTransitionResult:
    pipeline: AISTPipeline
    changed: bool
    old_status: str
    new_status: str


def is_terminal_pipeline_status(status: str) -> bool:
    return status in TERMINAL_PIPELINE_STATUSES


def pipeline_finished_at(pipeline: AISTPipeline) -> datetime | None:
    """When a terminal pipeline finished, ``None`` while it is not terminal; without ``finished_at`` it is ``updated``."""
    if not is_terminal_pipeline_status(pipeline.status):
        return None
    return pipeline.finished_at or pipeline.updated


def pipeline_elapsed(pipeline: AISTPipeline, now: datetime) -> timedelta:
    """A pipeline's run time: from launch to its finish, or to ``now`` while it is not terminal."""
    end = pipeline_finished_at(pipeline) or now
    return max(end - pipeline.created, timedelta(0))


def transition_allowed(current_status: str, new_status: str) -> bool:
    """Whether the lifecycle permits this move; a caller offers the action only when it does."""
    return new_status in PIPELINE_TRANSITIONS.get(current_status, frozenset())


def statuses_after(status: str) -> frozenset[str]:
    """Every status the lifecycle graph can reach from ``status``, other than ``status`` itself."""
    reached: set[str] = set()
    frontier = [status]
    while frontier:
        for successor in PIPELINE_TRANSITIONS.get(frontier.pop(), frozenset()):
            if successor not in reached:
                reached.add(successor)
                frontier.append(successor)
    reached.discard(status)
    return frozenset(reached)


def lock_pipeline(pipeline_id: str, *, related: Sequence[str] = ()) -> AISTPipeline | None:
    """
    Lock the pipeline row, and only that row, for the caller's transaction; None when the pipeline does not exist.

    ``related`` names relations to load in the same query; their rows are not locked.
    """
    pipelines = AISTPipeline.objects.select_for_update(of=("self",))
    if related:
        pipelines = pipelines.select_related(*related)
    return pipelines.filter(pk=pipeline_id).first()


def lock_existing_pipeline(pipeline_id: str, *, related: Sequence[str] = ()) -> AISTPipeline:
    """``lock_pipeline`` for a pipeline that must exist; raises ``AISTPipeline.DoesNotExist`` when it does not."""
    pipeline = lock_pipeline(pipeline_id, related=related)
    if pipeline is None:
        raise AISTPipeline.DoesNotExist(pipeline_id)
    return pipeline


def cancel_local_triage_batches(pipeline_id: str) -> None:
    """
    Cancel the pipeline's open local triage batches; the caller holds the pipeline's row lock.

    A pending batch is cancelled at once. A running batch is cancelling until its session ends, so the pipeline's
    sources stay in use meanwhile.
    """
    batches = LocalTriageBatch.objects.filter(pipeline_id=pipeline_id)
    now = timezone.now()
    batches.filter(state=LocalTriageBatchState.PENDING).update(state=LocalTriageBatchState.CANCELLED, updated=now)
    batches.filter(state=LocalTriageBatchState.RUNNING).update(state=LocalTriageBatchState.CANCELLING, updated=now)


def pipelines_with_released_sources() -> QuerySet[AISTPipeline]:
    """Finished pipelines without an open local triage batch: nothing reads their sources any more."""
    return AISTPipeline.objects.filter(status__in=TERMINAL_PIPELINE_STATUSES).exclude(
        local_triage_batches__state__in=OPEN_LOCAL_TRIAGE_BATCH_STATES,
    )


def delete_released_pipeline(pipeline_id: str) -> bool:
    """Delete the pipeline under its row lock when its sources are released; False when they are not."""
    with transaction.atomic():
        pipeline = lock_pipeline(pipeline_id)
        if pipeline is None or not pipelines_with_released_sources().filter(pk=pipeline.pk).exists():
            return False
        pipeline.delete()
        return True


def transition_pipeline_status(
    pipeline_id: str,
    new_status: str,
    *,
    field_updates: Mapping[str, object] | None = None,
    update_fields: Sequence[str] | None = None,
) -> PipelineTransitionResult:
    """
    Apply one idempotent, row-locked lifecycle transition.

    Status, lifecycle timestamps, task ownership, lease release, cancelling the local
    triage batches of a finished pipeline and the emitted transition event are
    committed as one unit. ``field_updates`` is intentionally
    explicit so callers cannot save a stale pipeline object around the transition.
    """
    if new_status not in PIPELINE_TRANSITIONS:
        detail = f"Unknown pipeline status: {new_status}."
        raise PipelineTransitionError(detail)

    with transaction.atomic():
        pipeline = lock_existing_pipeline(pipeline_id)
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
            cancel_local_triage_batches(pipeline.pk)

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
