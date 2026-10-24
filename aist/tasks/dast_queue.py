"""
Worker-side access to a DAST run's work queue.

The route to a gateway runs through the integration's VPN sidecar, which needs the Docker socket.
A worker has both; a web process has neither. These two tasks are therefore the only place AIST
reads or commands a run's queue, and everything a page shows comes from what they leave behind.
"""

from __future__ import annotations

import logging

from celery import shared_task

from aist.models import AISTPipeline
from aist.services.dast_queue import (
    CommandOutcome,
    DastQueueError,
    apply_command,
    refresh_snapshot,
    store_command_outcome,
)

logger = logging.getLogger(__name__)


@shared_task
def refresh_dast_queue(pipeline_id: str, *, async_user=None) -> None:
    """Read one run's queue and store the snapshot a page renders from."""
    del async_user  # unused; accepted only for the DojoAsyncTask contract
    pipeline = (
        AISTPipeline.objects
        .select_related("dast_binding__target__integration", "dast_execution_state")
        .filter(pk=pipeline_id)
        .first()
    )
    if pipeline is None:
        return
    try:
        refresh_snapshot(pipeline)
    except DastQueueError as exc:
        # A failure to read leaves the previous snapshot in place: an operator keeps the queue they
        # had, with its own age on it, rather than losing the view because one read failed.
        logger.warning("DAST queue snapshot not refreshed for %s: %s", pipeline_id, exc.code)


@shared_task
def apply_dast_queue_command(
    pipeline_id: str,
    *,
    action: str,
    item_id: str,
    values: dict,
    request_id: str,
    expected_revision: int,
    actor: str,
    async_user=None,
) -> None:
    """Send one operator command and leave its answer where the page that sent it will look."""
    del async_user  # unused; accepted only for the DojoAsyncTask contract
    pipeline = (
        AISTPipeline.objects
        .select_related("dast_binding__target__integration", "dast_execution_state")
        .filter(pk=pipeline_id)
        .first()
    )
    if pipeline is None:
        return
    try:
        outcome = apply_command(
            pipeline,
            action=action,
            item_id=item_id,
            values=values,
            request_id=request_id,
            expected_revision=expected_revision,
            actor=actor,
        )
    except DastQueueError as exc:
        outcome = CommandOutcome(
            state="done",
            accepted=False,
            refusal={"code": exc.code, "message": "", "field": "", "allowed": []},
        )
    store_command_outcome(pipeline_id, actor, request_id, outcome)
    # What the command did is only visible in the queue it changed, so read it again straight away.
    refresh_dast_queue.delay(pipeline_id)
