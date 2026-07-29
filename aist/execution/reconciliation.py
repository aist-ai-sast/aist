from __future__ import annotations

import logging
import uuid
from dataclasses import asdict, dataclass
from datetime import timedelta
from typing import TYPE_CHECKING

from celery import current_app, states
from celery.result import AsyncResult
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from aist.execution.contracts import PipelineTaskName
from aist.execution.leases import DEFAULT_EXECUTION_LEASE_POLICY, ExecutionLeasePolicy
from aist.execution.registry import execution_driver_registry
from aist.models import (
    AISTPipeline,
    AISTStatus,
    PipelineExecutionLease,
    PipelineLaunchRequest,
    PipelineLaunchRequestState,
)
from aist.utils.pipeline import is_terminal_pipeline_status, set_pipeline_status

if TYPE_CHECKING:
    from collections.abc import Callable

LOGGER = logging.getLogger(__name__)

DEFAULT_CLAIM_TIMEOUT = timedelta(minutes=5)
STALE_CLAIM_REQUEUED = "STALE_CLAIM_REQUEUED"
STALE_CLAIM_RECOVERED = "STALE_CLAIM_RECOVERED"
ORPHAN_PIPELINE = "ORPHAN_PIPELINE"
EXECUTION_LEASE_MISSING = "EXECUTION_LEASE_MISSING"
OUTBOX_LEASE_RENEWED = "OUTBOX_LEASE_RENEWED"
DEAD_EXECUTION_TASK = "DEAD_EXECUTION_TASK"
EXECUTION_RECOVERY_REPUBLISHED = "EXECUTION_RECOVERY_REPUBLISHED"
TERMINAL_LEASE_RELEASED = "TERMINAL_LEASE_RELEASED"

_STALE_CLAIM_DETAIL = "A stale dispatcher claim was returned to the durable launch queue."
_RECOVERED_CLAIM_DETAIL = "A stale dispatcher claim with a committed outbox was recovered for publication."
_ORPHAN_PIPELINE_DETAIL = "The launch request no longer has its committed pipeline."
_MISSING_LEASE_DETAIL = "The committed launch outbox no longer owns an active execution lease."
_RENEWED_OUTBOX_DETAIL = "The committed outbox lease was renewed while broker delivery remains recoverable."
_DEAD_TASK_DETAIL = "The dispatched execution task became terminal before its pipeline completed."
_RECOVERY_REPUBLISHED_DETAIL = "A recoverable provider run was republished through the generic executor."
_TERMINAL_RELEASE_DETAIL = "A lease left behind by a terminal pipeline was released."

_OUTBOX_STATES = {
    PipelineLaunchRequestState.PLANNED,
    PipelineLaunchRequestState.PUBLISHED,
}
_REQUEST_TERMINAL_STATES = {
    PipelineLaunchRequestState.SUPERSEDED,
    PipelineLaunchRequestState.FAILED,
    PipelineLaunchRequestState.EXPIRED,
    PipelineLaunchRequestState.CANCELLED,
}


@dataclass(slots=True)
class LaunchReconciliationStats:
    processed: int = 0
    requeued_claims: int = 0
    recovered_outboxes: int = 0
    failed_orphans: int = 0
    released_leases: int = 0
    reconciled_dead_tasks: int = 0
    resumed_executions: int = 0
    renewed_live_leases: int = 0
    skipped_live_owners: int = 0

    def as_dict(self) -> dict[str, int]:
        return asdict(self)


def _task_state(task_id: str) -> str | None:
    try:
        return AsyncResult(task_id).state
    except Exception:
        LOGGER.exception("Could not inspect launch task state (task_id=%s)", task_id)
        return None


def _lease_is_reclaimable(
    lease: PipelineExecutionLease,
    *,
    now,
    policy: ExecutionLeasePolicy,
) -> bool:
    return lease.expires_at <= now and lease.heartbeat_at <= now - policy.heartbeat_grace


def _release_leases(leases: list[PipelineExecutionLease], *, now) -> int:
    released = 0
    for lease in leases:
        if lease.released_at is None:
            lease.released_at = now
            lease.save(update_fields=["released_at"])
            released += 1
    return released


def _renew_leases(
    leases: list[PipelineExecutionLease],
    *,
    now,
    policy: ExecutionLeasePolicy,
    only_reclaimable: bool = False,
) -> int:
    """Extend lease ownership for work that is provably still in flight."""
    renewed = 0
    for lease in leases:
        if only_reclaimable and not _lease_is_reclaimable(lease, now=now, policy=policy):
            continue
        lease.heartbeat_at = now
        lease.expires_at = now + policy.ttl
        lease.save(update_fields=["heartbeat_at", "expires_at"])
        renewed += 1
    return renewed


def _audit_request(
    request: PipelineLaunchRequest,
    *,
    code: str,
    detail: str,
    state: str | None = None,
    clear_claim: bool = False,
) -> None:
    update_fields = ["failure_code", "failure_detail", "updated"]
    request.failure_code = code
    request.failure_detail = detail
    if state is not None:
        request.state = state
        update_fields.append("state")
    if clear_claim:
        request.claim_owner = None
        request.claimed_at = None
        update_fields.extend(["claim_owner", "claimed_at"])
    request.save(update_fields=update_fields)


def _mark_pipeline_reconciled_failure(
    pipeline: AISTPipeline,
    *,
    code: str,
    detail: str,
    now,
) -> None:
    launch_data = dict(pipeline.launch_data or {})
    launch_data["execution_reconciliation"] = {
        "code": code,
        "detail": detail,
        "reconciled_at": now.isoformat(),
    }
    pipeline.launch_data = launch_data
    if pipeline.status != AISTStatus.FINISHED_WITH_WARNINGS:
        set_pipeline_status(
            pipeline,
            AISTStatus.FINISHED_WITH_WARNINGS,
            update_fields_extra=["launch_data"],
        )
        return
    pipeline.run_task_id = None
    pipeline.save(update_fields=["launch_data", "run_task_id", "updated"])


def _outbox_is_valid(request: PipelineLaunchRequest, pipeline: AISTPipeline | None) -> bool:
    return bool(
        pipeline is not None
        and request.task_name
        and isinstance(request.task_args_snapshot, list)
        and pipeline.run_task_id == str(request.task_id),
    )


def _reconcile_locked_request(
    request: PipelineLaunchRequest,
    *,
    observed_task_state: str | None,
    now,
    claim_cutoff,
    lease_policy: ExecutionLeasePolicy,
    stats: LaunchReconciliationStats,
) -> None:
    leases = list(
        PipelineExecutionLease.objects.select_for_update()
        .filter(request=request, released_at__isnull=True)
        .order_by("resource_key", "slot", "pk"),
    )
    pipeline = None
    if request.pipeline_id is not None:
        pipeline = AISTPipeline.objects.select_for_update().filter(pk=request.pipeline_id).first()

    if request.state in _REQUEST_TERMINAL_STATES:
        stats.released_leases += _release_leases(leases, now=now)
        return

    if request.state == PipelineLaunchRequestState.PENDING:
        reclaimable = [
            lease for lease in leases
            if _lease_is_reclaimable(lease, now=now, policy=lease_policy)
        ]
        stats.released_leases += _release_leases(reclaimable, now=now)
        return

    if request.state == PipelineLaunchRequestState.CLAIMED:
        if request.claimed_at is None or request.claimed_at > claim_cutoff:
            return
        if any(not _lease_is_reclaimable(lease, now=now, policy=lease_policy) for lease in leases):
            stats.skipped_live_owners += 1
            return
        if _outbox_is_valid(request, pipeline) and leases:
            _renew_leases(leases, now=now, policy=lease_policy)
            _audit_request(
                request,
                code=STALE_CLAIM_RECOVERED,
                detail=_RECOVERED_CLAIM_DETAIL,
                state=PipelineLaunchRequestState.PLANNED,
            )
            stats.recovered_outboxes += 1
            return
        stats.released_leases += _release_leases(leases, now=now)
        if pipeline is not None:
            _mark_pipeline_reconciled_failure(
                pipeline,
                code=EXECUTION_LEASE_MISSING,
                detail=_MISSING_LEASE_DETAIL,
                now=now,
            )
            _audit_request(
                request,
                code=EXECUTION_LEASE_MISSING,
                detail=_MISSING_LEASE_DETAIL,
                state=PipelineLaunchRequestState.FAILED,
                clear_claim=True,
            )
            stats.failed_orphans += 1
            return
        request.not_before = now
        request.save(update_fields=["not_before", "updated"])
        _audit_request(
            request,
            code=STALE_CLAIM_REQUEUED,
            detail=_STALE_CLAIM_DETAIL,
            state=PipelineLaunchRequestState.PENDING,
            clear_claim=True,
        )
        stats.requeued_claims += 1
        return

    if request.state in _OUTBOX_STATES:
        if pipeline is None:
            stats.released_leases += _release_leases(leases, now=now)
            _audit_request(
                request,
                code=ORPHAN_PIPELINE,
                detail=_ORPHAN_PIPELINE_DETAIL,
                state=PipelineLaunchRequestState.FAILED,
                clear_claim=True,
            )
            stats.failed_orphans += 1
            return
        if not leases or not _outbox_is_valid(request, pipeline):
            stats.released_leases += _release_leases(leases, now=now)
            _mark_pipeline_reconciled_failure(
                pipeline,
                code=EXECUTION_LEASE_MISSING,
                detail=_MISSING_LEASE_DETAIL,
                now=now,
            )
            _audit_request(
                request,
                code=EXECUTION_LEASE_MISSING,
                detail=_MISSING_LEASE_DETAIL,
                state=PipelineLaunchRequestState.FAILED,
                clear_claim=True,
            )
            stats.failed_orphans += 1
            return
        renewed = _renew_leases(leases, now=now, policy=lease_policy, only_reclaimable=True)
        if renewed:
            _audit_request(request, code=OUTBOX_LEASE_RENEWED, detail=_RENEWED_OUTBOX_DETAIL)
            stats.recovered_outboxes += 1
        return

    if request.state != PipelineLaunchRequestState.DISPATCHED:
        return
    if pipeline is None:
        stats.released_leases += _release_leases(leases, now=now)
        _audit_request(
            request,
            code=ORPHAN_PIPELINE,
            detail=_ORPHAN_PIPELINE_DETAIL,
            state=PipelineLaunchRequestState.FAILED,
            clear_claim=True,
        )
        stats.failed_orphans += 1
        return
    pipeline_completed = is_terminal_pipeline_status(pipeline.status) and pipeline.run_task_id is None
    if pipeline_completed:
        released = _release_leases(leases, now=now)
        stats.released_leases += released
        if released:
            _audit_request(request, code=TERMINAL_LEASE_RELEASED, detail=_TERMINAL_RELEASE_DETAIL)
        return
    if observed_task_state not in states.READY_STATES:
        # The execution is still alive, so its capacity is legitimately held. Renew the lease
        # rather than leaving it expired-but-protected: a DAST run can outlive the lease TTL by
        # hours, and an un-renewed lease is indistinguishable from an abandoned one to anything
        # that only reads the lease table.
        if any(_lease_is_reclaimable(lease, now=now, policy=lease_policy) for lease in leases):
            stats.skipped_live_owners += 1
        stats.renewed_live_leases += _renew_leases(leases, now=now, policy=lease_policy)
        return
    driver = execution_driver_registry.resolve(pipeline.execution_type)
    if driver.should_recover(pipeline) and leases:
        recovery_task_id = uuid.uuid4()
        pipeline.run_task_id = str(recovery_task_id)
        pipeline.save(update_fields=["run_task_id", "updated"])
        for lease in leases:
            lease.heartbeat_at = now
            lease.expires_at = now + lease_policy.ttl
            lease.save(update_fields=["heartbeat_at", "expires_at"])
        request.task_id = recovery_task_id
        request.task_name = PipelineTaskName.RUN_PIPELINE_EXECUTION.value
        request.state = PipelineLaunchRequestState.PUBLISHED
        request.dispatched_at = None
        request.failure_code = EXECUTION_RECOVERY_REPUBLISHED
        request.failure_detail = _RECOVERY_REPUBLISHED_DETAIL
        request.save(update_fields=[
            "task_id",
            "task_name",
            "state",
            "dispatched_at",
            "failure_code",
            "failure_detail",
            "updated",
        ])
        transaction.on_commit(
            lambda: current_app.send_task(
                PipelineTaskName.RUN_PIPELINE_EXECUTION.value,
                args=[pipeline.id],
                task_id=str(recovery_task_id),
            ),
        )
        stats.resumed_executions += 1
        return
    _mark_pipeline_reconciled_failure(
        pipeline,
        code=DEAD_EXECUTION_TASK,
        detail=_DEAD_TASK_DETAIL,
        now=now,
    )
    stats.released_leases += _release_leases(leases, now=now)
    _audit_request(request, code=DEAD_EXECUTION_TASK, detail=_DEAD_TASK_DETAIL)
    stats.reconciled_dead_tasks += 1


def reconcile_launch_requests(
    *,
    now=None,
    claim_timeout: timedelta = DEFAULT_CLAIM_TIMEOUT,
    lease_policy: ExecutionLeasePolicy = DEFAULT_EXECUTION_LEASE_POLICY,
    batch_size: int = 200,
    task_state_getter: Callable[[str], str | None] = _task_state,
) -> dict[str, int]:
    """Repair launch/outbox/lease invariants without broker I/O inside DB locks."""
    reconciliation_time = now or timezone.now()
    claim_cutoff = reconciliation_time - claim_timeout
    request_ids = list(
        PipelineLaunchRequest.objects.filter(
            (
                Q(state=PipelineLaunchRequestState.CLAIMED)
                & (Q(claimed_at__isnull=True) | Q(claimed_at__lte=claim_cutoff))
            )
            | Q(state__in=_OUTBOX_STATES)
            | Q(state=PipelineLaunchRequestState.DISPATCHED, pipeline__run_task_id__isnull=False)
            | Q(execution_leases__isnull=False, execution_leases__released_at__isnull=True),
        )
        .distinct()
        .order_by("updated", "pk")
        .values_list("pk", flat=True)[:max(int(batch_size or 1), 1)],
    )
    stats = LaunchReconciliationStats()
    for request_id in request_ids:
        snapshot = PipelineLaunchRequest.objects.filter(pk=request_id).values(
            "state",
            "task_id",
        ).first()
        observed_task_state = None
        if snapshot and snapshot["state"] == PipelineLaunchRequestState.DISPATCHED:
            observed_task_state = task_state_getter(str(snapshot["task_id"]))
        with transaction.atomic():
            request = (
                PipelineLaunchRequest.objects.select_for_update(skip_locked=True)
                .filter(pk=request_id)
                .first()
            )
            if request is None:
                continue
            stats.processed += 1
            _reconcile_locked_request(
                request,
                observed_task_state=observed_task_state,
                now=reconciliation_time,
                claim_cutoff=claim_cutoff,
                lease_policy=lease_policy,
                stats=stats,
            )
    return stats.as_dict()
