from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping


_ERR_AUTHORITY_ORGANIZATION = "Launch authority requires a valid organization id."
_ERR_AUTHORITY_PAT_TOKEN = "PAT launch authority requires a public token record id."  # noqa: S105
_ERR_AUTHORITY_NON_PAT_TOKEN = "Only PAT launch authority may reference a token record."  # noqa: S105
_ERR_PROJECT = "Execution plan requires a valid project id."
_ERR_RESOURCE_KEY = "Execution plan resource key must not be empty."
_ERR_RESOURCE_LIMIT = "Execution plan resource limit must be at least one."
_ERR_COALESCE_KEY = "Execution plan coalesce key must not be empty."
_ERR_EFFECTIVE_REQUIRED = "Preselected-version policy requires an effective project version."
_ERR_RESULT_TRIGGER_REQUIRED = "Result-resolved version policy requires a trigger project version."
_ERR_RESULT_VERSION = "Result-resolved version policy cannot preselect an effective version."
_ERR_PULL_REQUEST = "Execution plan pull request id must be a positive integer."


class PipelineExecutionKind(StrEnum):
    SAST = "SAST"
    DAST = "DAST"


class PipelineTaskName(StrEnum):
    RUN_SAST_PIPELINE = "aist.tasks.pipeline.run_sast_pipeline"
    RUN_PIPELINE_EXECUTION = "aist.tasks.pipeline.run_pipeline_execution"


class EffectiveVersionPolicy(StrEnum):
    PRESELECT_EFFECTIVE_VERSION = "PRESELECT_EFFECTIVE_VERSION"
    RESOLVE_FROM_EXECUTION_RESULT = "RESOLVE_FROM_EXECUTION_RESULT"


class LaunchSource(StrEnum):
    MANUAL = "MANUAL"
    SCHEDULE = "SCHEDULE"
    SCM_WEBHOOK = "SCM_WEBHOOK"
    RECONCILER = "RECONCILER"


class LaunchAuthorityKind(StrEnum):
    USER = "USER"
    PAT = "PAT"
    SCHEDULE = "SCHEDULE"
    SCM_WEBHOOK = "SCM_WEBHOOK"
    RECONCILER = "RECONCILER"


class ExecutionPlanError(ValueError):

    """Raised when trusted adapter code produces an invalid execution plan."""


@dataclass(frozen=True, slots=True)
class LaunchAuthority:

    """Secret-free identity used to authorize and audit a launch."""

    kind: LaunchAuthorityKind
    source: LaunchSource
    organization_id: int
    requester_id: int | None = None
    api_token_id: int | None = None

    def __post_init__(self) -> None:
        if self.organization_id < 1:
            raise ExecutionPlanError(_ERR_AUTHORITY_ORGANIZATION)
        if self.kind == LaunchAuthorityKind.PAT and self.api_token_id is None:
            raise ExecutionPlanError(_ERR_AUTHORITY_PAT_TOKEN)
        if self.kind != LaunchAuthorityKind.PAT and self.api_token_id is not None:
            raise ExecutionPlanError(_ERR_AUTHORITY_NON_PAT_TOKEN)


@dataclass(frozen=True, slots=True)
class LaunchPlanningContext:

    """Persistence-neutral input supplied to a trusted launch adapter."""

    launch_request_id: int
    execution_type: PipelineExecutionKind
    project_id: int
    trigger_project_version_id: int | None
    params_snapshot: Mapping[str, object]
    capability_snapshot: Mapping[str, object]
    authority: LaunchAuthority


@dataclass(frozen=True, slots=True)
class ExecutionPlan:

    """Immutable plan consumed by the generic pipeline publisher."""

    execution_type: PipelineExecutionKind
    task_name: PipelineTaskName
    task_args: tuple[object, ...]
    project_id: int
    trigger_project_version_id: int | None
    effective_version_policy: EffectiveVersionPolicy
    effective_project_version_id: int | None
    resource_key: str
    resource_limit: int
    coalesce_key: str | None
    initial_launch_data: Mapping[str, object]
    authority: LaunchAuthority
    pull_request_id: int | None = None

    def __post_init__(self) -> None:
        if self.project_id < 1:
            raise ExecutionPlanError(_ERR_PROJECT)
        if not self.resource_key.strip():
            raise ExecutionPlanError(_ERR_RESOURCE_KEY)
        if self.resource_limit < 1:
            raise ExecutionPlanError(_ERR_RESOURCE_LIMIT)
        if self.coalesce_key is not None and not self.coalesce_key.strip():
            raise ExecutionPlanError(_ERR_COALESCE_KEY)
        if self.pull_request_id is not None and (
            isinstance(self.pull_request_id, bool) or self.pull_request_id < 1
        ):
            raise ExecutionPlanError(_ERR_PULL_REQUEST)
        if self.effective_version_policy == EffectiveVersionPolicy.PRESELECT_EFFECTIVE_VERSION:
            if self.effective_project_version_id is None:
                raise ExecutionPlanError(_ERR_EFFECTIVE_REQUIRED)
        elif self.effective_project_version_id is not None:
            raise ExecutionPlanError(_ERR_RESULT_VERSION)
        elif self.trigger_project_version_id is None:
            raise ExecutionPlanError(_ERR_RESULT_TRIGGER_REQUIRED)
