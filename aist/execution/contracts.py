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
_ERR_NO_VERSION_TRIGGER = "No-version policy cannot carry a trigger project version."
_ERR_PULL_REQUEST = "Execution plan pull request id must be a positive integer."
_ERR_METRIC_LABEL = "Execution metric label is invalid."
_ERR_METRIC_OPERATIONS = "Execution metric operations are invalid."


class PipelineExecutionKind(StrEnum):
    SAST = "SAST"
    DAST = "DAST"


class PipelineTaskName(StrEnum):
    RUN_PIPELINE_EXECUTION = "aist.tasks.pipeline.run_pipeline_execution"


class ProviderOperation(StrEnum):

    """
    Every operation an execution provider can be observed performing.

    One vocabulary for the provider side, the adapter descriptors and the metrics. Three
    disagreeing sets meant an unrecognised operation was silently relabelled "execute", so a
    resumed run was indistinguishable from a first attempt in the very metric used to diagnose
    resume loops.
    """

    PING = "ping"
    CATALOG = "catalog"
    EXECUTE = "execute"
    RESUME = "resume"
    CANCEL = "cancel"
    # Reads a run's final status without starting one; distinct from RESUME so the two are
    # separable in the metric.
    HARVEST = "harvest"
    RECONCILE = "reconcile"


class ExecutionCancellationMode(StrEnum):
    IMMEDIATE = "IMMEDIATE"
    COOPERATIVE = "COOPERATIVE"


@dataclass(frozen=True, slots=True)
class ExecutionMetricDescriptor:
    label: str
    operations: frozenset[str]

    def __post_init__(self) -> None:
        if not self.label or len(self.label) > 24 or not self.label.replace("_", "").isalnum():
            raise ExecutionPlanError(_ERR_METRIC_LABEL)
        if not self.operations or any(not operation or len(operation) > 24 for operation in self.operations):
            raise ExecutionPlanError(_ERR_METRIC_OPERATIONS)
        unknown = {operation for operation in self.operations if operation not in set(ProviderOperation)}
        if unknown:
            detail = f"{_ERR_METRIC_OPERATIONS} Unknown: {sorted(unknown)}."
            raise ExecutionPlanError(detail)


class EffectiveVersionPolicy(StrEnum):
    PRESELECT_EFFECTIVE_VERSION = "PRESELECT_EFFECTIVE_VERSION"
    RESOLVE_FROM_EXECUTION_RESULT = "RESOLVE_FROM_EXECUTION_RESULT"
    #: Neither now nor later: a sourceless DAST run has no project-version concept at all.
    NO_VERSION = "NO_VERSION"


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

    def __init__(self, detail: str, *, code: str = "PLANNING_REJECTED"):
        self.code = code[:64]
        self.safe_detail = str(detail)[:512]
        super().__init__(self.safe_detail)


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
        elif self.effective_version_policy == EffectiveVersionPolicy.RESOLVE_FROM_EXECUTION_RESULT:
            if self.trigger_project_version_id is None:
                raise ExecutionPlanError(_ERR_RESULT_TRIGGER_REQUIRED)
        elif self.trigger_project_version_id is not None:
            raise ExecutionPlanError(_ERR_NO_VERSION_TRIGGER)
