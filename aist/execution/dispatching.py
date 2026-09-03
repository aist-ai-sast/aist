from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import timedelta
from enum import StrEnum
from typing import TYPE_CHECKING

from django.db import transaction
from django.utils import timezone

from aist.execution.contracts import ExecutionPlanError
from aist.execution.launch_request import LaunchRequestSnapshotError, validated_secret_free_json
from aist.execution.leases import acquire_execution_plan_lease, release_execution_lease
from aist.execution.observability import observe_lease_decision, record_queue_event
from aist.execution.retry import (
    DEFAULT_LAUNCH_RETRY_POLICY,
    LAUNCH_MAX_AGE_EXCEEDED,
    LAUNCH_MAX_AGE_FAILURE_DETAIL,
    LaunchRetryPolicy,
    capacity_backoff_seconds,
)
from aist.execution.sast import planning_context_from_launch_request
from aist.models import (
    AISTPipeline,
    AISTProjectVersion,
    AISTStatus,
    PipelineExecutionLease,
    PipelineLaunchRequest,
    PipelineLaunchRequestState,
)
from aist.services.pipeline_lifecycle import transition_pipeline_status
from aist.utils.pipeline import has_unfinished_pipeline

if TYPE_CHECKING:
    from aist.execution.adapters import LaunchAdapterRegistry

PLANNING_FAILED = "PLANNING_FAILED"
_PLANNING_FAILURE_DETAIL = "The launch request could not be planned safely."
_ERR_CLAIM = "Launch request is no longer owned by this dispatcher."
_ERR_OUTBOX = "Launch request has an invalid persisted outbox payload."
LOGGER = logging.getLogger(__name__)


class LaunchPlanningStatus(StrEnum):
    READY = "READY"
    BUSY = "BUSY"
    FAILED = "FAILED"


class LaunchAcceptance(StrEnum):
    ACCEPTED = "ACCEPTED"
    DUPLICATE = "DUPLICATE"
    REJECTED = "REJECTED"


class LaunchDispatchError(RuntimeError):

    """Raised when persisted outbox state violates the dispatcher contract."""


class _ExecutionBusyError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class LaunchPlanningResult:
    status: LaunchPlanningStatus
    request_id: int


@dataclass(frozen=True, slots=True)
class LaunchPublishCommand:
    request_id: int
    pipeline_id: str
    task_name: str
    task_id: str
    task_args: tuple[object, ...]


def _mark_claim_failed(
    *,
    request_id: int,
    claim_owner: str,
    code: str = PLANNING_FAILED,
    detail: str = _PLANNING_FAILURE_DETAIL,
) -> None:
    PipelineLaunchRequest.objects.filter(
        pk=request_id,
        state=PipelineLaunchRequestState.CLAIMED,
        claim_owner=claim_owner,
    ).update(
        state=PipelineLaunchRequestState.FAILED,
        failure_code=code[:64],
        failure_detail=detail[:512],
        updated=timezone.now(),
    )


def _defer_capacity_blocked_claim(
    *,
    request_id: int,
    claim_owner: str,
    policy: LaunchRetryPolicy = DEFAULT_LAUNCH_RETRY_POLICY,
    now=None,
) -> None:
    retry_time = now or timezone.now()
    with transaction.atomic():
        request = (
            PipelineLaunchRequest.objects
            .select_for_update()
            .filter(
                pk=request_id,
                state=PipelineLaunchRequestState.CLAIMED,
                claim_owner=claim_owner,
            )
            .first()
        )
        if request is None:
            return
        deadline = request.expires_at or request.created + policy.max_age
        if retry_time >= deadline:
            request.state = PipelineLaunchRequestState.EXPIRED
            request.failure_code = LAUNCH_MAX_AGE_EXCEEDED
            request.failure_detail = LAUNCH_MAX_AGE_FAILURE_DETAIL
        else:
            request.capacity_retry_count += 1
            delay_seconds = capacity_backoff_seconds(
                retry_count=request.capacity_retry_count,
                policy=policy,
            )
            request.state = PipelineLaunchRequestState.PENDING
            request.not_before = min(retry_time + timedelta(seconds=delay_seconds), deadline)
        request.expires_at = deadline
        request.claim_owner = None
        request.claimed_at = None
        request.save(update_fields=[
            "state",
            "not_before",
            "expires_at",
            "capacity_retry_count",
            "claim_owner",
            "claimed_at",
            "failure_code",
            "failure_detail",
            "updated",
        ])
        event = "expired" if request.state == PipelineLaunchRequestState.EXPIRED else "capacity_wait"
        transaction.on_commit(
            lambda: record_queue_event(execution_type=request.execution_type, event=event),
        )


def _persist_execution_plan(*, request_id: int, claim_owner: str, lease_id: int, plan, adapter_registry) -> None:
    task_args = validated_secret_free_json(list(plan.task_args), label="task_args_snapshot")
    if not isinstance(task_args, list):
        raise LaunchRequestSnapshotError(_ERR_OUTBOX)

    with transaction.atomic():
        request = (
            PipelineLaunchRequest.objects
            .select_for_update()
            .select_related("project__product__prod_type")
            .get(
                pk=request_id,
                state=PipelineLaunchRequestState.CLAIMED,
                claim_owner=claim_owner,
                pipeline__isnull=True,
            )
        )
        lease = PipelineExecutionLease.objects.select_for_update().get(
            pk=lease_id,
            request_id=request_id,
            released_at__isnull=True,
            pipeline__isnull=True,
        )
        if request.project_id != plan.project_id or request.execution_type != plan.execution_type.value:
            raise LaunchDispatchError(_ERR_CLAIM)

        project_version = None
        if plan.effective_project_version_id is not None:
            project_version = AISTProjectVersion.objects.select_for_update().get(
                pk=plan.effective_project_version_id,
                project_id=request.project_id,
            )
            if has_unfinished_pipeline(project_version):
                raise _ExecutionBusyError

        pipeline = AISTPipeline.objects.create(
            id=request.task_id.hex,
            project_id=request.project_id,
            project_version=project_version,
            trigger_project_version_id=plan.trigger_project_version_id,
            dast_binding_id=request.dast_binding_id,
            execution_type=plan.execution_type.value,
            status=AISTStatus.ADMITTED,
            launch_data=dict(plan.initial_launch_data),
            run_task_id=str(request.task_id),
            pull_request_id=plan.pull_request_id,
        )
        adapter_registry.initialize_pipeline(pipeline)
        lease.pipeline = pipeline
        lease.save(update_fields=["pipeline"])
        request.pipeline = pipeline
        request.task_name = plan.task_name.value
        request.task_args_snapshot = task_args
        request.coalesce_key = plan.coalesce_key
        request.state = PipelineLaunchRequestState.PLANNED
        request.failure_code = ""
        request.failure_detail = ""
        request.save(update_fields=[
            "pipeline",
            "task_name",
            "task_args_snapshot",
            "coalesce_key",
            "state",
            "failure_code",
            "failure_detail",
            "updated",
        ])


def plan_claimed_launch(
    *,
    request_id: int,
    claim_owner: str,
    adapter_registry: LaunchAdapterRegistry,
) -> LaunchPlanningResult:
    """Build, lease, and persist one execution plan without touching the broker."""
    try:
        request = (
            PipelineLaunchRequest.objects
            .select_related(
                "api_token",
                "project__product__prod_type",
                "requester",
                "schedule",
                "launch_config",
                "trigger_project_version",
            )
            .get(
                pk=request_id,
                state=PipelineLaunchRequestState.CLAIMED,
                claim_owner=claim_owner,
            )
        )
        plan = adapter_registry.build_plan(planning_context_from_launch_request(request))
    except ExecutionPlanError as exc:
        _mark_claim_failed(
            request_id=request_id,
            claim_owner=claim_owner,
            code=exc.code,
            detail=exc.safe_detail,
        )
        return LaunchPlanningResult(status=LaunchPlanningStatus.FAILED, request_id=request_id)
    except Exception:
        LOGGER.exception("Unexpected launch planning failure (request_id=%s)", request_id)
        _mark_claim_failed(request_id=request_id, claim_owner=claim_owner)
        return LaunchPlanningResult(status=LaunchPlanningStatus.FAILED, request_id=request_id)

    lease = acquire_execution_plan_lease(
        request_id=request_id,
        claim_owner=claim_owner,
        plan=plan,
    )
    observe_lease_decision(
        execution_type=plan.execution_type.value,
        acquired_slot=lease.slot if lease is not None else None,
        capacity=plan.resource_limit,
    )
    if lease is None:
        _defer_capacity_blocked_claim(request_id=request_id, claim_owner=claim_owner)
        return LaunchPlanningResult(status=LaunchPlanningStatus.BUSY, request_id=request_id)

    try:
        _persist_execution_plan(
            request_id=request_id,
            claim_owner=claim_owner,
            lease_id=lease.pk,
            plan=plan,
            adapter_registry=adapter_registry,
        )
    except _ExecutionBusyError:
        release_execution_lease(lease_id=lease.pk, request_id=request_id)
        _defer_capacity_blocked_claim(request_id=request_id, claim_owner=claim_owner)
        return LaunchPlanningResult(status=LaunchPlanningStatus.BUSY, request_id=request_id)
    except ExecutionPlanError as exc:
        release_execution_lease(lease_id=lease.pk, request_id=request_id)
        _mark_claim_failed(
            request_id=request_id,
            claim_owner=claim_owner,
            code=exc.code,
            detail=exc.safe_detail,
        )
        return LaunchPlanningResult(status=LaunchPlanningStatus.FAILED, request_id=request_id)
    except Exception:
        LOGGER.exception("Unexpected launch plan persistence failure (request_id=%s)", request_id)
        release_execution_lease(lease_id=lease.pk, request_id=request_id)
        _mark_claim_failed(request_id=request_id, claim_owner=claim_owner)
        return LaunchPlanningResult(status=LaunchPlanningStatus.FAILED, request_id=request_id)
    return LaunchPlanningResult(status=LaunchPlanningStatus.READY, request_id=request_id)


def prepare_launch_publish(*, request_id: int) -> LaunchPublishCommand:
    """Persist publish intent before returning the immutable broker command."""
    with transaction.atomic():
        request = (
            PipelineLaunchRequest.objects
            .select_for_update()
            .get(pk=request_id)
        )
        if request.state == PipelineLaunchRequestState.PLANNED:
            request.state = PipelineLaunchRequestState.PUBLISHED
            request.save(update_fields=["state", "updated"])
        elif request.state != PipelineLaunchRequestState.PUBLISHED:
            raise LaunchDispatchError(_ERR_OUTBOX)
        if (
            request.pipeline_id is None
            or not request.task_name
            or not isinstance(request.task_args_snapshot, list)
            or request.pipeline.run_task_id != str(request.task_id)
        ):
            raise LaunchDispatchError(_ERR_OUTBOX)
        return LaunchPublishCommand(
            request_id=request.pk,
            pipeline_id=request.pipeline_id,
            task_name=request.task_name,
            task_id=str(request.task_id),
            task_args=tuple(request.task_args_snapshot),
        )


def accept_published_launch(*, pipeline_id: str, task_id: str | None) -> LaunchAcceptance:
    """Atomically admit the first broker delivery and reject every duplicate."""
    with transaction.atomic():
        request = (
            PipelineLaunchRequest.objects
            .select_for_update()
            .filter(pipeline_id=pipeline_id)
            .first()
        )
        if request is None:
            return LaunchAcceptance.REJECTED
        if task_id is None or str(request.task_id) != str(task_id):
            return LaunchAcceptance.REJECTED
        if request.state == PipelineLaunchRequestState.PUBLISHED:
            request.state = PipelineLaunchRequestState.DISPATCHED
            request.dispatched_at = timezone.now()
            request.save(update_fields=["state", "dispatched_at", "updated"])
            transition_pipeline_status(pipeline_id, AISTStatus.EXECUTING)
            return LaunchAcceptance.ACCEPTED
        if request.state == PipelineLaunchRequestState.DISPATCHED:
            return LaunchAcceptance.DUPLICATE
        return LaunchAcceptance.REJECTED
