from __future__ import annotations

from dataclasses import dataclass

from django.db import transaction
from django.utils import timezone
from dojo.authorization.roles_permissions import Permissions

from aist.execution.observability import observe_queue_claim, record_queue_event
from aist.execution.retry import LAUNCH_MAX_AGE_EXCEEDED, LAUNCH_MAX_AGE_FAILURE_DETAIL
from aist.models import (
    ApiTokenScope,
    PipelineLaunchAuthorityKind,
    PipelineLaunchOrigin,
    PipelineLaunchRequest,
    PipelineLaunchRequestState,
)
from aist.queries import get_authorized_aist_projects

AUTHORITY_REVOKED = "AUTHORITY_REVOKED"
_AUTHORITY_FAILURE_DETAIL = "Launch authority is no longer valid for this project."


@dataclass(frozen=True, slots=True)
class ClaimResult:
    request_id: int
    claim_owner: str


def claim_next_launch_request(*, claim_owner: str, now=None) -> ClaimResult | None:
    """Atomically claim one ready request without planning, network, or broker work."""
    owner = claim_owner.strip()
    if not owner:
        msg = "Claim owner must not be blank."
        raise ValueError(msg)
    claimed_at = now or timezone.now()
    with transaction.atomic():
        expired_count = PipelineLaunchRequest.objects.filter(
            state=PipelineLaunchRequestState.PENDING,
            expires_at__isnull=False,
            expires_at__lte=claimed_at,
        ).update(
            state=PipelineLaunchRequestState.EXPIRED,
            failure_code=LAUNCH_MAX_AGE_EXCEEDED,
            failure_detail=LAUNCH_MAX_AGE_FAILURE_DETAIL,
            updated=claimed_at,
        )
        if expired_count:
            transaction.on_commit(
                lambda: record_queue_event(execution_type="all", event="expired", amount=expired_count),
            )
        request = (
            PipelineLaunchRequest.objects
            .select_for_update(skip_locked=True)
            .filter(
                state=PipelineLaunchRequestState.PENDING,
                not_before__lte=claimed_at,
            )
            .order_by("-priority", "created", "pk")
            .first()
        )
        if request is None:
            return None
        request.state = PipelineLaunchRequestState.CLAIMED
        request.claim_owner = owner
        request.claimed_at = claimed_at
        request.save(update_fields=["state", "claim_owner", "claimed_at", "updated"])
        queue_age = (claimed_at - request.created).total_seconds()
        transaction.on_commit(
            lambda: observe_queue_claim(execution_type=request.execution_type, age_seconds=queue_age),
        )
        return ClaimResult(request_id=request.pk, claim_owner=owner)


def _user_can_operate_project(request: PipelineLaunchRequest) -> bool:
    user = request.requester
    return bool(
        user is not None
        and user.is_active
        and get_authorized_aist_projects(Permissions.Product_Edit, user=user)
        .filter(pk=request.project_id)
        .exists(),
    )


def _pat_can_operate_project(request: PipelineLaunchRequest) -> bool:
    token = request.api_token
    if (
        token is None
        or request.requester_id != token.user_id
        or token.organization_id != request.project.organization_id
        or token.scope != ApiTokenScope.READ_WRITE
        or not token.is_usable
        or not token.user.is_active
    ):
        return False
    token.user._aist_token_organization_id = token.organization_id
    return get_authorized_aist_projects(Permissions.Product_Edit, user=token.user).filter(
        pk=request.project_id,
    ).exists()


def _stored_authority_is_valid(request: PipelineLaunchRequest) -> bool:
    if request.authority_kind == PipelineLaunchAuthorityKind.USER:
        return request.origin == PipelineLaunchOrigin.MANUAL and _user_can_operate_project(request)
    if request.authority_kind == PipelineLaunchAuthorityKind.PAT:
        return request.origin == PipelineLaunchOrigin.MANUAL and _pat_can_operate_project(request)
    if request.authority_kind == PipelineLaunchAuthorityKind.SCHEDULE:
        return bool(
            request.origin == PipelineLaunchOrigin.SCHEDULE
            and request.schedule_id
            and request.schedule.enabled
            and request.launch_config_id == request.schedule.launch_config_id
            and request.launch_config.project_id == request.project_id,
        )
    if request.authority_kind == PipelineLaunchAuthorityKind.SCM_WEBHOOK:
        return bool(
            request.origin == PipelineLaunchOrigin.SCM_WEBHOOK
            and request.project.repository_id
            and (
                request.trigger_project_version_id is None
                or request.trigger_project_version.project_id == request.project_id
            ),
        )
    if request.authority_kind == PipelineLaunchAuthorityKind.RECONCILER:
        return request.origin == PipelineLaunchOrigin.RECONCILER
    return False


def revalidate_claimed_authority(*, request_id: int, claim_owner: str) -> bool:
    """Revalidate persisted authority after claim and fail closed on revocation."""
    try:
        request = (
            PipelineLaunchRequest.objects
            .select_related(
                "api_token__user",
                "requester",
                "project__product__prod_type",
                "project__repository",
                "schedule__launch_config",
                "launch_config",
                "trigger_project_version",
            )
            .get(
                pk=request_id,
                state=PipelineLaunchRequestState.CLAIMED,
                claim_owner=claim_owner,
            )
        )
    except PipelineLaunchRequest.DoesNotExist:
        return False
    if _stored_authority_is_valid(request):
        return PipelineLaunchRequest.objects.filter(
            pk=request_id,
            state=PipelineLaunchRequestState.CLAIMED,
            claim_owner=claim_owner,
        ).exists()
    with transaction.atomic():
        PipelineLaunchRequest.objects.filter(
            pk=request_id,
            state=PipelineLaunchRequestState.CLAIMED,
            claim_owner=claim_owner,
        ).update(
            state=PipelineLaunchRequestState.FAILED,
            failure_code=AUTHORITY_REVOKED,
            failure_detail=_AUTHORITY_FAILURE_DETAIL,
            updated=timezone.now(),
        )
    return False
