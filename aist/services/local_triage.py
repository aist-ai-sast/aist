"""
Local AI triage in bounded batches.

A pipeline's findings that need triage are split into batches of ``AIST_LOCAL_TRIAGE_BATCH_SIZE``; a Celery chain
triages them one bridge session per batch, so the size of a pipeline never changes how closely each finding is read.
A finding needs triage while it is a triage candidate of the pipeline and the pipeline holds no AI response for it.
Batch state changes happen under the pipeline's row lock.
"""
from __future__ import annotations

from itertools import batched
from typing import TYPE_CHECKING

from django.conf import settings
from django.db import transaction
from django.db.models import Max, QuerySet
from dojo.models import Finding

from aist.models import LocalTriageBatch, LocalTriageBatchState
from aist.services.ai_triage import triage_candidates
from aist.services.pipeline_lifecycle import OPEN_LOCAL_TRIAGE_BATCH_STATES, cancel_local_triage_batches, lock_pipeline
from aist.utils.ai_response import ai_answered_finding_ids

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping


class LocalTriageInProgressError(Exception):

    """The pipeline already has triage batches open."""


def unanswered_finding_ids(pipeline_id: str, finding_ids: Iterable[int] | QuerySet) -> list[int]:
    """Those of ``finding_ids`` for which the pipeline holds no AI response, ascending."""
    return list(
        Finding.objects.filter(id__in=finding_ids)
        .exclude(id__in=ai_answered_finding_ids(pipeline_id=pipeline_id))
        .order_by("id").values_list("id", flat=True),
    )


def plan_local_triage(pipeline_id: str, finding_ids: Iterable[int] | QuerySet) -> list[LocalTriageBatch]:
    """
    Queue those of ``finding_ids`` that need triage in the pipeline as batches after its existing ones.

    Raises ``LocalTriageInProgressError`` while the pipeline has open batches.
    """
    with transaction.atomic():
        pipeline = lock_pipeline(pipeline_id)
        if pipeline is None:
            return []
        if LocalTriageBatch.objects.filter(pipeline_id=pipeline_id, state__in=OPEN_LOCAL_TRIAGE_BATCH_STATES).exists():
            raise LocalTriageInProgressError(pipeline_id)
        ids = unanswered_finding_ids(pipeline_id, triage_candidates(pipeline).filter(id__in=finding_ids).values("id"))
        last = LocalTriageBatch.objects.filter(pipeline_id=pipeline_id).aggregate(last=Max("sequence"))["last"] or 0
        return LocalTriageBatch.objects.bulk_create(
            LocalTriageBatch(pipeline_id=pipeline_id, sequence=sequence, finding_ids=list(chunk))
            for sequence, chunk in enumerate(
                batched(ids, settings.AIST_LOCAL_TRIAGE_BATCH_SIZE, strict=False), start=last + 1,
            )
        )


def _move_batch(
    pipeline_id: str, batch_id: int, moves: Mapping[LocalTriageBatchState, LocalTriageBatchState],
) -> LocalTriageBatch | None:
    """Move the batch from a state in ``moves`` to the state it maps to; None when the batch is in none of them."""
    with transaction.atomic():
        if lock_pipeline(pipeline_id) is None:
            return None
        batch = LocalTriageBatch.objects.filter(pk=batch_id, pipeline_id=pipeline_id, state__in=list(moves)).first()
        if batch is not None:
            batch.state = moves[batch.state]
            batch.save(update_fields=["state", "updated"])
        return batch


def start_batch(pipeline_id: str, batch_id: int) -> LocalTriageBatch | None:
    """
    Mark the batch running; None when it may not start.

    A pending batch starts. A running batch is returned as well: its task is being redelivered after its worker was
    lost. A cancelling batch lost its session with that worker and becomes cancelled; a completed or cancelled batch
    stays as it is.
    """
    batch = _move_batch(pipeline_id, batch_id, {
        LocalTriageBatchState.PENDING: LocalTriageBatchState.RUNNING,
        LocalTriageBatchState.RUNNING: LocalTriageBatchState.RUNNING,
        LocalTriageBatchState.CANCELLING: LocalTriageBatchState.CANCELLED,
    })
    return batch if batch is not None and batch.state == LocalTriageBatchState.RUNNING else None


def end_batch(pipeline_id: str, batch_id: int) -> LocalTriageBatch | None:
    """
    End the batch's session: a running batch is completed, a cancelling one cancelled.

    None when the batch is neither, e.g. a duplicate delivery of its task already ended it.
    """
    return _move_batch(pipeline_id, batch_id, {
        LocalTriageBatchState.RUNNING: LocalTriageBatchState.COMPLETED,
        LocalTriageBatchState.CANCELLING: LocalTriageBatchState.CANCELLED,
    })


def cancel_open_batches(pipeline_id: str) -> None:
    """Cancel the pipeline's open batches under its row lock, e.g. when the pipeline is already finished."""
    with transaction.atomic():
        if lock_pipeline(pipeline_id) is not None:
            cancel_local_triage_batches(pipeline_id)


def untriaged_finding_ids(pipeline_id: str) -> list[int]:
    """The findings of the pipeline's completed batches for which it holds no AI response."""
    batch_finding_ids = [
        finding_id
        for ids in LocalTriageBatch.objects.filter(
            pipeline_id=pipeline_id, state=LocalTriageBatchState.COMPLETED,
        ).values_list("finding_ids", flat=True)
        for finding_id in ids
    ]
    return unanswered_finding_ids(pipeline_id, batch_finding_ids)
