from __future__ import annotations

from dataclasses import dataclass

from celery import shared_task

from aist.execution.adapters import UnknownLaunchAdapterError
from aist.execution.contracts import ProviderOperation
from aist.execution.dispatching import LaunchAcceptance, accept_published_launch
from aist.execution.registry import execution_driver_registry
from aist.models import (
    AISTPipeline,
    DastExecutionState,
)
from aist.tasks.dast_execution import run_dast_execution
from aist.tasks.sast_execution import run_sast_execution
from aist.utils.pipeline_imports import cleanup_pipeline_containers


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


@shared_task
def stop_pipeline_execution(pipeline_id: str, *, async_user_id=None) -> None:
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


# A provider run is terminal only after an explicit domain outcome. Transport recovery is
# therefore unbounded; last_progress_at remains telemetry and never becomes a stop decision.
@shared_task(bind=True, max_retries=None)
def run_pipeline_execution(self, pipeline_id: str, async_user_id=None) -> None:
    """Execute one persisted pipeline; the broker carries only ``pipeline_id``."""
    del async_user_id
    execution_type = AISTPipeline.objects.values_list("execution_type", flat=True).get(pk=pipeline_id)
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
            return None
    return driver.invoke(_WorkerExecutionRuntime(self), pipeline_id)
