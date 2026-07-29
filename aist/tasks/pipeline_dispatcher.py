import logging
import uuid

from celery import shared_task
from django.conf import settings

from aist.execution.claiming import claim_next_launch_request, revalidate_claimed_authority
from aist.execution.dispatching import (
    LaunchPlanningStatus,
    LaunchPublishCommand,
    plan_claimed_launch,
    prepare_launch_publish,
)
from aist.execution.registry import execution_driver_registry
from aist.models import PipelineLaunchRequest, PipelineLaunchRequestState
from aist.tasks.pipeline import run_pipeline_execution

logger = logging.getLogger("aist")
launch_adapter_registry = execution_driver_registry
_MAX_DISPATCH_BATCH_SIZE = 200


def _publish(command: LaunchPublishCommand) -> None:
    if command.task_name != run_pipeline_execution.name:
        message = f"Invalid generic execution task {command.task_name}."
        raise ValueError(message)
    if command.task_args:
        message = "Generic execution messages may contain only pipeline_id."
        raise ValueError(message)
    run_pipeline_execution.apply_async(
        args=(command.pipeline_id,),
        task_id=command.task_id,
    )


def _validated_batch_size(batch_size: int | None) -> int:
    value = (
        getattr(settings, "AIST_PIPELINE_DISPATCH_BATCH_SIZE", 50)
        if batch_size is None
        else batch_size
    )
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= _MAX_DISPATCH_BATCH_SIZE:
        message = f"Dispatcher batch_size must be an integer between 1 and {_MAX_DISPATCH_BATCH_SIZE}."
        raise ValueError(message)
    return value


def _republish_outbox(*, batch_size: int) -> None:
    request_ids = list(
        PipelineLaunchRequest.objects.filter(
            state__in=[
                PipelineLaunchRequestState.PLANNED,
                PipelineLaunchRequestState.PUBLISHED,
            ],
        ).order_by("created", "pk").values_list("pk", flat=True)[:batch_size],
    )
    for request_id in request_ids:
        try:
            _publish(prepare_launch_publish(request_id=request_id))
        except Exception:
            logger.exception("Dispatcher: failed to republish launch request=%s", request_id)


@shared_task(name="aist.tasks.pipeline_dispatcher.dispatch_queued_pipelines")
def dispatch_queued_pipelines(async_user=None, batch_size=None):
    """Publish durable generic launch requests without holding DB locks across broker I/O."""
    del async_user
    resolved_batch_size = _validated_batch_size(batch_size)
    _republish_outbox(batch_size=resolved_batch_size)
    claim_owner = f"pipeline-dispatcher:{uuid.uuid4()}"

    for _index in range(resolved_batch_size):
        claim = claim_next_launch_request(claim_owner=claim_owner)
        if claim is None:
            break
        if not revalidate_claimed_authority(
            request_id=claim.request_id,
            claim_owner=claim.claim_owner,
        ):
            continue
        result = plan_claimed_launch(
            request_id=claim.request_id,
            claim_owner=claim.claim_owner,
            adapter_registry=launch_adapter_registry,
        )
        if result.status == LaunchPlanningStatus.BUSY:
            logger.info("Dispatcher: execution capacity unavailable for request=%s", claim.request_id)
            continue
        if result.status == LaunchPlanningStatus.FAILED:
            logger.error("Dispatcher: planning failed for request=%s", claim.request_id)
            continue
        try:
            _publish(prepare_launch_publish(request_id=claim.request_id))
        except Exception:
            logger.exception(
                "Dispatcher: broker publication is ambiguous for request=%s; leaving outbox recoverable.",
                claim.request_id,
            )
