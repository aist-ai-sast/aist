from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING

from django.db import IntegrityError, transaction
from django.utils import timezone

from aist.execution.observability import operational_alert
from aist.models import PipelineExecutionLease, PipelineLaunchRequest, PipelineLaunchRequestState

if TYPE_CHECKING:
    from aist.execution.contracts import ExecutionPlan

_ERR_CAPACITY = "Resource capacity must be at least one."
_ERR_GRACE = "Heartbeat grace must not be negative."
_ERR_RESOURCE_KEY = "Resource key must not be empty."
_ERR_TTL = "Lease TTL must be positive."
_ERR_PLAN_REQUEST = "Execution plan does not match the claimed launch request."


class ExecutionLeaseError(ValueError):

    """Raised when a caller violates the generic lease contract."""


@dataclass(frozen=True, slots=True)
class ExecutionLeasePolicy:

    ttl: timedelta
    heartbeat_grace: timedelta

    def __post_init__(self) -> None:
        if self.ttl <= timedelta(0):
            raise ExecutionLeaseError(_ERR_TTL)
        if self.heartbeat_grace < timedelta(0):
            raise ExecutionLeaseError(_ERR_GRACE)


DEFAULT_EXECUTION_LEASE_POLICY = ExecutionLeasePolicy(
    ttl=timedelta(minutes=5),
    heartbeat_grace=timedelta(minutes=1),
)


def acquire_execution_plan_lease(
    *,
    request_id: int,
    claim_owner: str,
    plan: ExecutionPlan,
    policy: ExecutionLeasePolicy = DEFAULT_EXECUTION_LEASE_POLICY,
    now=None,
) -> PipelineExecutionLease | None:
    """Acquire capacity derived exclusively from a trusted execution plan."""
    lease_time = now or timezone.now()
    normalized_key = plan.resource_key.strip()
    if not normalized_key:
        raise ExecutionLeaseError(_ERR_RESOURCE_KEY)
    if plan.resource_limit < 1:
        raise ExecutionLeaseError(_ERR_CAPACITY)
    with transaction.atomic():
        request = PipelineLaunchRequest.objects.select_for_update().filter(
            pk=request_id,
            state=PipelineLaunchRequestState.CLAIMED,
            claim_owner=claim_owner,
            project_id=plan.project_id,
            execution_type=plan.execution_type.value,
        ).first()
        if request is None or request.project.organization_id != plan.authority.organization_id:
            raise ExecutionLeaseError(_ERR_PLAN_REQUEST)
        for slot in range(plan.resource_limit):
            try:
                with transaction.atomic():
                    return PipelineExecutionLease.objects.create(
                        resource_key=normalized_key,
                        slot=slot,
                        request=request,
                        acquired_at=lease_time,
                        heartbeat_at=lease_time,
                        expires_at=lease_time + policy.ttl,
                    )
            except IntegrityError:
                continue
    return None


def release_execution_lease(*, lease_id: int, request_id: int, now=None) -> bool:
    """Idempotently release ownership while following request-before-lease lock order."""
    release_time = now or timezone.now()
    with transaction.atomic():
        PipelineLaunchRequest.objects.select_for_update().get(pk=request_id)
        lease = PipelineExecutionLease.objects.select_for_update().filter(
            pk=lease_id,
            request_id=request_id,
        ).first()
        if lease is None or lease.released_at is not None:
            return False
        lease.released_at = release_time
        lease.save(update_fields=["released_at"])
        return True


def report_stale_execution_leases(
    *,
    policy: ExecutionLeasePolicy = DEFAULT_EXECUTION_LEASE_POLICY,
    now=None,
) -> int:
    """
    Alert on leases whose owner stopped renewing them, without releasing anything.

    Releasing is the request state machine's job: only it can tell a recoverable outbox or a
    live dispatched execution apart from genuinely abandoned capacity. This function exists so
    that a lease nobody renews still becomes visible in monitoring rather than silently
    occupying a slot until the next reconciliation pass notices.
    """
    observation_time = now or timezone.now()
    stale = PipelineExecutionLease.objects.filter(
        released_at__isnull=True,
        expires_at__lte=observation_time,
        heartbeat_at__lte=observation_time - policy.heartbeat_grace,
    ).count()
    if stale:
        operational_alert(code="lease_stale", execution_type="all", count=stale)
    return stale
