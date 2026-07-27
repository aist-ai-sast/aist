from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from dataclasses import dataclass
from typing import TYPE_CHECKING

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.utils import timezone

from aist.execution.contracts import LaunchAuthorityKind, LaunchSource, PipelineExecutionKind
from aist.execution.dast import build_dast_coalesce_key
from aist.execution.launch_request import (
    LaunchRequestSnapshotError,
    LaunchRequestSnapshots,
    validated_secret_free_json,
)
from aist.execution.observability import AuditContext, audit_event, record_queue_event
from aist.execution.retry import DEFAULT_LAUNCH_RETRY_POLICY
from aist.execution.sast import build_sast_coalesce_key, resolve_effective_sast_schedule
from aist.models import (
    AISTApiToken,
    AISTProject,
    AISTProjectLaunchConfig,
    AISTProjectVersion,
    LaunchSchedule,
    Organization,
    PipelineLaunchRequest,
    PipelineLaunchRequestState,
)
from aist.pipeline_args import PipelineArguments

if TYPE_CHECKING:
    from collections.abc import Mapping

    from django.contrib.auth.base_user import AbstractBaseUser


_MAX_CLIENT_REQUEST_KEY_LENGTH = 255
_ERR_ORGANIZATION = "Launch principal requires a persisted organization."
_ERR_PROJECT_ORGANIZATION = "Launch project must belong to the launch principal organization."
_ERR_USER = "User launch principal requires a persisted requester."
_ERR_PAT = "PAT launch principal requires a persisted token owned by the requester and organization."
_ERR_SYSTEM_PRINCIPAL = "System launch principal cannot carry a requester or PAT."
_ERR_AUTHORITY_SOURCE = "Launch authority kind is not valid for its source."
_ERR_CONFIG_PROJECT = "Launch configuration must belong to the launch project."
_ERR_SCHEDULE_CONFIG = "Launch schedule must belong to the launch configuration."
_ERR_TRIGGER_PROJECT = "Trigger project version must belong to the launch project."
_ERR_DAST_CONFIG = "DAST launch requests require a launch configuration with an explicit binding."
_ERR_CLIENT_KEY = "Client request key must be a non-blank string of at most 255 characters."
_ERR_IDEMPOTENCY_CONFLICT = "Client request key was already used with a different launch snapshot."
_ERR_INITIAL_LAUNCH_DATA = "initial_launch_data_snapshot must be a JSON object."


class LaunchEnqueueError(ValueError):

    """Raised when trusted producer context cannot form a valid launch request."""


class LaunchIdempotencyConflictError(LaunchEnqueueError):

    """Raised when an idempotency key is replayed with a different immutable request."""


@dataclass(frozen=True, slots=True)
class LaunchPrincipal:

    """Server-built, secret-free producer identity; HTTP payloads never construct this value."""

    organization: Organization
    kind: LaunchAuthorityKind
    source: LaunchSource
    requester: AbstractBaseUser | None = None
    api_token: AISTApiToken | None = None

    def __post_init__(self) -> None:
        if self.organization.pk is None:
            raise LaunchEnqueueError(_ERR_ORGANIZATION)
        if self.kind == LaunchAuthorityKind.USER:
            if self.requester is None or self.requester.pk is None or self.api_token is not None:
                raise LaunchEnqueueError(_ERR_USER)
        elif self.kind == LaunchAuthorityKind.PAT:
            if (
                self.requester is None
                or self.requester.pk is None
                or self.api_token is None
                or self.api_token.pk is None
                or self.api_token.user_id != self.requester.pk
                or self.api_token.organization_id != self.organization.pk
            ):
                raise LaunchEnqueueError(_ERR_PAT)
        elif self.requester is not None or self.api_token is not None:
            raise LaunchEnqueueError(_ERR_SYSTEM_PRINCIPAL)

        expected_kind = {
            LaunchSource.SCHEDULE: LaunchAuthorityKind.SCHEDULE,
            LaunchSource.SCM_WEBHOOK: LaunchAuthorityKind.SCM_WEBHOOK,
            LaunchSource.RECONCILER: LaunchAuthorityKind.RECONCILER,
        }.get(self.source)
        if expected_kind is not None and self.kind != expected_kind:
            raise LaunchEnqueueError(_ERR_AUTHORITY_SOURCE)
        if self.source == LaunchSource.MANUAL and self.kind not in {
            LaunchAuthorityKind.USER,
            LaunchAuthorityKind.PAT,
        }:
            raise LaunchEnqueueError(_ERR_AUTHORITY_SOURCE)

    @classmethod
    def for_user(
        cls,
        *,
        organization: Organization,
        requester: AbstractBaseUser,
        api_token: AISTApiToken | None = None,
    ) -> LaunchPrincipal:
        return cls(
            organization=organization,
            kind=LaunchAuthorityKind.PAT if api_token is not None else LaunchAuthorityKind.USER,
            source=LaunchSource.MANUAL,
            requester=requester,
            api_token=api_token,
        )

    @classmethod
    def for_schedule(cls, *, organization: Organization) -> LaunchPrincipal:
        return cls(
            organization=organization,
            kind=LaunchAuthorityKind.SCHEDULE,
            source=LaunchSource.SCHEDULE,
        )

    @classmethod
    def for_scm_webhook(cls, *, organization: Organization) -> LaunchPrincipal:
        return cls(
            organization=organization,
            kind=LaunchAuthorityKind.SCM_WEBHOOK,
            source=LaunchSource.SCM_WEBHOOK,
        )


@dataclass(frozen=True, slots=True)
class EnqueueResult:
    request: PipelineLaunchRequest
    created: bool


def _client_request_key_hash(
    *,
    principal: LaunchPrincipal,
    project_id: int,
    client_request_key: str | None,
) -> str | None:
    if client_request_key is None:
        return None
    if (
        not isinstance(client_request_key, str)
        or not client_request_key.strip()
        or len(client_request_key) > _MAX_CLIENT_REQUEST_KEY_LENGTH
    ):
        raise LaunchEnqueueError(_ERR_CLIENT_KEY)
    namespace = {
        "organization_id": principal.organization.pk,
        "project_id": project_id,
        "source": principal.source.value,
        "kind": principal.kind.value,
        "requester_id": principal.requester.pk if principal.requester is not None else None,
        "api_token_id": principal.api_token.pk if principal.api_token is not None else None,
        "client_request_key": client_request_key,
    }
    canonical = json.dumps(namespace, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(canonical.encode()).hexdigest()


def _snapshots_for_launch(
    *,
    project: AISTProject,
    execution_type: PipelineExecutionKind,
    launch_config: AISTProjectLaunchConfig | None,
    raw_params: Mapping[str, object],
) -> LaunchRequestSnapshots:
    params = deepcopy(dict(launch_config.params)) if launch_config is not None else {}
    params.update(deepcopy(dict(raw_params)))
    if execution_type == PipelineExecutionKind.SAST:
        params = PipelineArguments.normalize_params(project=project, raw_params=params)
        capability = {}
    else:
        if launch_config is None or launch_config.dast_binding_id is None:
            raise LaunchEnqueueError(_ERR_DAST_CONFIG)
        capability = launch_config.dast_binding.target.get_snapshot().to_snapshot()
    return LaunchRequestSnapshots.from_values(params=params, capability=capability)


def _matches_existing(existing: PipelineLaunchRequest, values: dict[str, object]) -> bool:
    comparable_fields = (
        "origin",
        "execution_type",
        "project_id",
        "dast_binding_id",
        "trigger_project_version_id",
        "schedule_id",
        "launch_config_id",
        "requester_id",
        "api_token_id",
        "authority_kind",
        "params_snapshot",
        "capability_snapshot",
        "initial_launch_data_snapshot",
        "coalesce_key",
    )
    return all(getattr(existing, field) == values[field] for field in comparable_fields)


def enqueue_pipeline_launch(
    *,
    project: AISTProject,
    principal: LaunchPrincipal,
    raw_params: Mapping[str, object],
    execution_type: PipelineExecutionKind = PipelineExecutionKind.SAST,
    launch_config: AISTProjectLaunchConfig | None = None,
    schedule: LaunchSchedule | None = None,
    trigger_project_version: AISTProjectVersion | None = None,
    client_request_key: str | None = None,
    initial_launch_data: Mapping[str, object] | None = None,
) -> EnqueueResult:
    """Validate, normalize, freeze, and atomically persist one durable launch intent."""
    if project.pk is None or project.organization_id != principal.organization.pk:
        raise LaunchEnqueueError(_ERR_PROJECT_ORGANIZATION)
    if launch_config is not None:
        if launch_config.project_id != project.pk:
            raise LaunchEnqueueError(_ERR_CONFIG_PROJECT)
        execution_type = PipelineExecutionKind(launch_config.execution_type)
    if schedule is not None and (launch_config is None or schedule.launch_config_id != launch_config.pk):
        raise LaunchEnqueueError(_ERR_SCHEDULE_CONFIG)
    if trigger_project_version is not None and trigger_project_version.project_id != project.pk:
        raise LaunchEnqueueError(_ERR_TRIGGER_PROJECT)

    try:
        snapshots = _snapshots_for_launch(
            project=project,
            execution_type=execution_type,
            launch_config=launch_config,
            raw_params=raw_params,
        )
    except (TypeError, ValueError, ValidationError, AISTProjectVersion.DoesNotExist) as exc:
        raise LaunchEnqueueError(str(exc)) from exc
    dast_binding = launch_config.dast_binding if execution_type == PipelineExecutionKind.DAST else None
    params_snapshot = snapshots.params_snapshot()
    capability_snapshot = snapshots.capability_snapshot()
    try:
        initial_launch_data_snapshot = validated_secret_free_json(
            dict(initial_launch_data or {}),
            label="initial_launch_data_snapshot",
        )
    except (LaunchRequestSnapshotError, TypeError, ValueError) as exc:
        raise LaunchEnqueueError(str(exc)) from exc
    if not isinstance(initial_launch_data_snapshot, dict):
        raise LaunchEnqueueError(_ERR_INITIAL_LAUNCH_DATA)
    coalesce_key = None
    if execution_type == PipelineExecutionKind.SAST:
        coalesce_key = build_sast_coalesce_key(
            project_id=project.pk,
            effective_project_version_id=(params_snapshot.get("project_version") or {}).get("id"),
            params_snapshot=params_snapshot,
            initial_launch_data_snapshot=initial_launch_data_snapshot,
            schedule=resolve_effective_sast_schedule(schedule=schedule, launch_config=launch_config),
        )
    else:
        coalesce_key = build_dast_coalesce_key(
            project_id=project.pk,
            binding_id=dast_binding.pk,
            integration_id=dast_binding.target.integration_id,
            params_snapshot=params_snapshot,
            capability_snapshot=capability_snapshot,
        )
    key_hash = _client_request_key_hash(
        principal=principal,
        project_id=project.pk,
        client_request_key=client_request_key,
    )
    values: dict[str, object] = {
        "origin": principal.source.value,
        "execution_type": execution_type.value,
        "project_id": project.pk,
        "dast_binding_id": dast_binding.pk if dast_binding is not None else None,
        "trigger_project_version_id": trigger_project_version.pk if trigger_project_version is not None else None,
        "schedule_id": schedule.pk if schedule is not None else None,
        "launch_config_id": launch_config.pk if launch_config is not None else None,
        "requester_id": principal.requester.pk if principal.requester is not None else None,
        "api_token_id": principal.api_token.pk if principal.api_token is not None else None,
        "authority_kind": principal.kind.value,
        "params_snapshot": params_snapshot,
        "capability_snapshot": capability_snapshot,
        "initial_launch_data_snapshot": initial_launch_data_snapshot,
        "coalesce_key": coalesce_key,
        "client_request_key_hash": key_hash,
        "expires_at": timezone.now() + DEFAULT_LAUNCH_RETRY_POLICY.max_age,
    }

    with transaction.atomic():
        AISTProject.objects.select_for_update().only("pk").get(pk=project.pk)
        if key_hash is not None:
            existing = PipelineLaunchRequest.objects.filter(client_request_key_hash=key_hash).first()
            if existing is not None:
                if not _matches_existing(existing, values):
                    raise LaunchIdempotencyConflictError(_ERR_IDEMPOTENCY_CONFLICT)
                return EnqueueResult(request=existing, created=False)
        request = PipelineLaunchRequest(**values)
        try:
            request.full_clean(validate_unique=False)
            with transaction.atomic():
                request.save(force_insert=True)
        except IntegrityError:
            if key_hash is None:
                raise
            existing = PipelineLaunchRequest.objects.get(client_request_key_hash=key_hash)
            if not _matches_existing(existing, values):
                raise LaunchIdempotencyConflictError(_ERR_IDEMPOTENCY_CONFLICT) from None
            return EnqueueResult(request=existing, created=False)
        except ValidationError as exc:
            raise LaunchEnqueueError(str(exc)) from exc
        if coalesce_key is not None:
            superseded = (
                PipelineLaunchRequest.objects
                .select_for_update()
                .filter(
                    project_id=project.pk,
                    coalesce_key=coalesce_key,
                    state=PipelineLaunchRequestState.PENDING,
                )
                .exclude(pk=request.pk)
            )
            superseded_count = superseded.update(
                state=PipelineLaunchRequestState.SUPERSEDED,
                superseded_by=request,
                failure_code="SUPERSEDED",
                failure_detail=f"Superseded by launch request {request.pk}.",
            )
            if superseded_count:
                transaction.on_commit(
                    lambda: record_queue_event(
                        execution_type=execution_type.value,
                        event="coalesced",
                        amount=superseded_count,
                    ),
                )
        transaction.on_commit(
            lambda: audit_event(
                "pipeline_launch_enqueued",
                context=AuditContext(
                    organization_id=principal.organization.pk,
                    project_id=project.pk,
                    binding_id=dast_binding.pk if dast_binding is not None else None,
                    request_id=request.pk,
                    actor_id=principal.requester.pk if principal.requester is not None else None,
                ),
            ),
        )
    return EnqueueResult(request=request, created=True)
