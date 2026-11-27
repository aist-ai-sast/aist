from __future__ import annotations

import logging
from dataclasses import dataclass
from math import inf

from celery import shared_task
from celery.exceptions import Ignore
from django.conf import settings

from aist.execution.adapters import UnknownLaunchAdapterError
from aist.execution.contracts import ProviderOperation
from aist.execution.dispatching import LaunchAcceptance, accept_published_launch
from aist.execution.registry import execution_driver_registry
from aist.models import (
    AISTPipeline,
    DastExecutionState,
)
from aist.services.pipeline_lifecycle import TERMINAL_PIPELINE_STATUSES
from aist.tasks.dast_execution import run_dast_execution
from aist.tasks.sast_execution import run_sast_execution
from aist.utils.pipeline_imports import cleanup_pipeline_containers

logger = logging.getLogger(__name__)
_STOP_RETRY_SECONDS = 15


@dataclass(frozen=True, slots=True)
class _WorkerExecutionRuntime:
    task: object

    def run_sast(self, pipeline_id: str):
        return run_sast_execution(pipeline_id)

    def run_dast(self, pipeline_id: str):
        # A run that already has a provider run id is resumed, not started; the metric counts the two apart.
        resuming = DastExecutionState.objects.filter(
            pipeline_id=pipeline_id,
        ).exclude(run_id="").exclude(run_id=None).exists()
        operation = ProviderOperation.RESUME if resuming else ProviderOperation.EXECUTE
        return run_dast_execution(self.task, pipeline_id, operation=operation)


@shared_task(bind=True, max_retries=None)
def stop_pipeline_execution(self, pipeline_id: str, *, async_user_id=None) -> None:
    """
    Signal a running execution's containers from the host that owns them.

    Whoever handled the operator's request persists the cancellation, and that is the web process.
    The containers belong to the worker: it is the only one with the Docker socket. Sweeping from
    anywhere else fails quietly -- the sweep sees no containers, the connector never gets its
    SIGTERM, and the provider run carries on while the platform reports a stop that was requested
    and never delivered.
    """
    del async_user_id  # unused; accepted only for the DojoAsyncTask contract
    cleanup_pipeline_containers(pipeline_id)
    if DastExecutionState.objects.filter(
        pipeline_id=pipeline_id,
        cancel_requested_at__isnull=False,
    ).exclude(pipeline__status__in=TERMINAL_PIPELINE_STATUSES).exists():
        # A sweep may land between observers; keep delivering the persisted operator decision.
        raise self.retry(countdown=_STOP_RETRY_SECONDS)


# Billiard also falls back to the pool limit for 0, so an unbounded execution needs infinity.
@shared_task(
    bind=True, max_retries=None,
    time_limit=settings.AIST_PIPELINE_TASK_TIME_LIMIT or inf,
    soft_time_limit=0,
)
def run_pipeline_execution(self, pipeline_id: str, async_user_id=None) -> None:
    """Execute one persisted SAST or DAST pipeline with its own worker limit."""
    del async_user_id
    execution_type = AISTPipeline.objects.filter(pk=pipeline_id).values_list("execution_type", flat=True).first()
    if execution_type is None:
        logger.info("Ignoring execution for deleted pipeline %s", pipeline_id)
        raise Ignore
    try:
        driver = execution_driver_registry.resolve(execution_type)
    except UnknownLaunchAdapterError as exc:
        detail = "Persisted pipeline execution type is not executable."
        raise ValueError(detail) from exc
    acceptance = accept_published_launch(
        pipeline_id=pipeline_id,
        task_id=getattr(self.request, "id", None),
    )
    if acceptance is LaunchAcceptance.REJECTED:
        return None
    if acceptance is LaunchAcceptance.DUPLICATE:
        if not driver.allows_duplicate_delivery(
            pipeline_id=pipeline_id,
            task_id=getattr(self.request, "id", None),
            retries=int(getattr(self.request, "retries", 0) or 0),
        ):
            # A normal return would overwrite the live delivery's backend state with SUCCESS.
            raise Ignore
    return driver.invoke(_WorkerExecutionRuntime(self), pipeline_id)
