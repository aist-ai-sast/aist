from __future__ import annotations

from typing import TYPE_CHECKING

from aist.execution.coalescing import canonical_coalesce_key
from aist.execution.contracts import (
    EffectiveVersionPolicy,
    ExecutionCancellationMode,
    ExecutionMetricDescriptor,
    ExecutionPlan,
    ExecutionPlanError,
    LaunchAuthority,
    LaunchAuthorityKind,
    LaunchPlanningContext,
    LaunchSource,
    PipelineExecutionKind,
    PipelineTaskName,
)
from aist.models import (
    AISTProjectLaunchConfig,
    AISTProjectVersion,
    LaunchSchedule,
    PipelineLaunchRequest,
    PullRequest,
)
from aist.pipeline_args import PipelineArguments

if TYPE_CHECKING:
    from collections.abc import Mapping


_ERR_REQUEST = "SAST launch request is unavailable in the authorized organization."
_ERR_CAPACITY = "SAST launch capacity must be at least one."
_ERR_VERSION = "Normalized SAST parameters require a project version from the same project."
_ERR_EXECUTION_TYPE = "SAST launch adapter received a different execution type."
_ERR_PULL_REQUEST = "SAST launch request references an invalid pull request."


def sast_resource_key(*, schedule: LaunchSchedule | None, project_id: int) -> str:
    """
    Resolve the capacity resource a SAST launch competes for.

    A schedule owns its own slot (distinct schedules must not serialize against each
    other); a manual run without a schedule competes only against other manual runs of
    the same project, not against every manual SAST launch in the installation.
    """
    if schedule is not None:
        return f"sast-schedule:{schedule.pk}"
    return f"sast-project:{project_id}"


def resolve_effective_sast_schedule(
    *,
    schedule: LaunchSchedule | None,
    launch_config: AISTProjectLaunchConfig | None,
) -> LaunchSchedule | None:
    """
    Resolve the schedule whose capacity a SAST launch actually draws from.

    A request created directly against a schedule already carries it. A manual "run
    now" launch against a launch_config that happens to have its own attached schedule
    resolves to that same schedule too, so it draws from (and is limited by) the
    schedule's slot rather than a separate per-project pool. Both enqueue time and plan
    time must derive this identically, or the coalesce key computed at enqueue won't
    match the resource key the request is later dispatched under.
    """
    if schedule is not None:
        return schedule
    if launch_config is not None:
        return launch_config.get_launch_schedule()
    return None


def build_sast_coalesce_key(
    *,
    project_id: int,
    effective_project_version_id: int | None,
    params_snapshot: Mapping[str, object],
    initial_launch_data_snapshot: Mapping[str, object] | None = None,
    schedule: LaunchSchedule | None = None,
) -> str:
    coalesce_params = dict(params_snapshot)
    if initial_launch_data_snapshot:
        coalesce_params["__aist_initial_launch_data"] = dict(initial_launch_data_snapshot)
    return canonical_coalesce_key(
        execution_type=PipelineExecutionKind.SAST,
        project_id=project_id,
        executor_identity={
            "resource_key": sast_resource_key(schedule=schedule, project_id=project_id),
            "effective_project_version_id": effective_project_version_id,
        },
        params_snapshot=coalesce_params,
        capability_snapshot={},
    )


def planning_context_from_launch_request(request: PipelineLaunchRequest) -> LaunchPlanningContext:
    snapshots = request.get_snapshots()
    organization_id = request.project.organization_id
    if organization_id is None:
        raise ExecutionPlanError(_ERR_REQUEST)
    try:
        execution_type = PipelineExecutionKind(request.execution_type)
        authority = LaunchAuthority(
            kind=LaunchAuthorityKind(request.authority_kind),
            source=LaunchSource(request.origin),
            organization_id=organization_id,
            requester_id=request.requester_id,
            api_token_id=request.api_token_id,
        )
    except ValueError as exc:
        raise ExecutionPlanError(str(exc)) from exc
    return LaunchPlanningContext(
        launch_request_id=request.pk,
        execution_type=execution_type,
        project_id=request.project_id,
        trigger_project_version_id=request.trigger_project_version_id,
        params_snapshot=snapshots.params,
        capability_snapshot=snapshots.capability,
        authority=authority,
    )


class SastPipelineLaunchAdapter:
    execution_type = PipelineExecutionKind.SAST
    cancellation_mode = ExecutionCancellationMode.IMMEDIATE
    metric_descriptor = ExecutionMetricDescriptor(
        label="sast",
        operations=frozenset({"execute", "cancel"}),
    )

    @staticmethod
    def initialize_pipeline(pipeline) -> None:
        del pipeline

    @staticmethod
    def allows_duplicate_delivery(*, pipeline_id: str, task_id: str | None, retries: int) -> bool:
        del pipeline_id, task_id, retries
        return False

    @staticmethod
    def should_recover(pipeline) -> bool:
        del pipeline
        return False

    @staticmethod
    def invoke(runtime, pipeline_id: str):
        return runtime.run_sast(pipeline_id)

    def build_plan(self, context: LaunchPlanningContext) -> ExecutionPlan:
        if context.execution_type != self.execution_type:
            raise ExecutionPlanError(_ERR_EXECUTION_TYPE)
        try:
            request = (
                PipelineLaunchRequest.objects
                .select_related(
                    "project__product__prod_type",
                    "schedule",
                    "launch_config__launch_schedule",
                )
                .get(
                    pk=context.launch_request_id,
                    project_id=context.project_id,
                    project__product__prod_type__aist_organization__id=context.authority.organization_id,
                )
            )
        except PipelineLaunchRequest.DoesNotExist as exc:
            raise ExecutionPlanError(_ERR_REQUEST) from exc
        schedule = resolve_effective_sast_schedule(schedule=request.schedule, launch_config=request.launch_config)
        resource_limit = int(schedule.max_concurrent_runs) if schedule is not None else 1
        if resource_limit < 1:
            raise ExecutionPlanError(_ERR_CAPACITY)

        params = PipelineArguments.normalize_params(
            project=request.project,
            raw_params=request.get_snapshots().params_snapshot(),
        )
        project_version_id = (params.get("project_version") or {}).get("id")
        try:
            project_version = AISTProjectVersion.objects.get(
                pk=project_version_id,
                project_id=request.project_id,
                project__product__prod_type__aist_organization__id=context.authority.organization_id,
            )
        except (AISTProjectVersion.DoesNotExist, TypeError, ValueError) as exc:
            raise ExecutionPlanError(_ERR_VERSION) from exc

        initial_launch_data = request.get_initial_launch_data_snapshot()
        pull_request_id = initial_launch_data.get("pull_request_id")
        if pull_request_id is not None:
            if isinstance(pull_request_id, bool) or not isinstance(pull_request_id, int):
                raise ExecutionPlanError(_ERR_PULL_REQUEST)
            try:
                pull_request_id = PullRequest.objects.only("pk").get(
                    pk=pull_request_id,
                    project_version=project_version,
                    repository_id=request.project.repository_id,
                ).pk
            except PullRequest.DoesNotExist as exc:
                raise ExecutionPlanError(_ERR_PULL_REQUEST) from exc

        coalesce_key = request.coalesce_key
        if not coalesce_key:
            detail = "SAST launch request is missing its frozen execution identity."
            raise ExecutionPlanError(detail)
        if request.launch_config_id is not None:
            params["launch_config_id"] = request.launch_config_id
        return ExecutionPlan(
            execution_type=self.execution_type,
            task_name=PipelineTaskName.RUN_PIPELINE_EXECUTION,
            task_args=(),
            project_id=request.project_id,
            trigger_project_version_id=None,
            effective_version_policy=EffectiveVersionPolicy.PRESELECT_EFFECTIVE_VERSION,
            effective_project_version_id=project_version.id,
            resource_key=sast_resource_key(schedule=schedule, project_id=request.project_id),
            resource_limit=resource_limit,
            coalesce_key=coalesce_key,
            initial_launch_data=initial_launch_data,
            authority=context.authority,
            pull_request_id=pull_request_id,
        )
