from __future__ import annotations

from django.db import transaction
from django.utils import timezone

from aist.models import PipelineExecutionLease, PipelineLaunchRequest, PipelineLaunchRequestState

# Requests in these states have not yet been handed to a worker; cancelling here only
# needs to flip the state and release any lease it already holds. DISPATCHED requests
# are out of scope on purpose: the caller must go through stop_pipeline() instead,
# because a worker may already be executing against the pipeline.
CANCELLABLE_STATES = frozenset({
    PipelineLaunchRequestState.PENDING,
    PipelineLaunchRequestState.CLAIMED,
    PipelineLaunchRequestState.PLANNED,
    PipelineLaunchRequestState.PUBLISHED,
})


def cancel_launch_request(*, request_id: int, now=None) -> bool:
    """
    Atomically cancel a not-yet-dispatched request and release any lease it holds.

    Returns False when the request does not exist or has already left a cancellable
    state (raced with a claim/dispatch/expiry, or already terminal) — callers should
    treat that as a 409, not retry.
    """
    cancel_time = now or timezone.now()
    with transaction.atomic():
        exists = (
            PipelineLaunchRequest.objects
            .select_for_update()
            .filter(pk=request_id, state__in=CANCELLABLE_STATES)
            .exists()
        )
        if not exists:
            return False
        PipelineExecutionLease.objects.select_for_update().filter(
            request_id=request_id,
            released_at__isnull=True,
        ).update(released_at=cancel_time)
        updated = PipelineLaunchRequest.objects.filter(
            pk=request_id,
            state__in=CANCELLABLE_STATES,
        ).update(state=PipelineLaunchRequestState.CANCELLED, updated=cancel_time)
        return bool(updated)
