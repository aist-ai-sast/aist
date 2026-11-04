from __future__ import annotations

from typing import TYPE_CHECKING

from django.db import transaction

from aist.execution.coalescing import canonical_coalesce_key
from aist.execution.contracts import (
    EffectiveVersionPolicy,
    ExecutionCancellationMode,
    ExecutionMetricDescriptor,
    ExecutionPlan,
    ExecutionPlanError,
    LaunchPlanningContext,
    PipelineExecutionKind,
    PipelineTaskName,
    ProviderOperation,
)
from aist.integrations.dast_config import (
    DastBindingParameters,
    DastConfigError,
    DastExecutionSnapshot,
    DastTargetSnapshot,
)
from aist.integrations.dast_readiness import check_dast_binding_readiness
from aist.launch_data import PipelineLaunchData
from aist.models import (
    DastExecutionOutcome,
    DastExecutionState,
    DastIntegrationState,
    PipelineLaunchRequest,
    PipelineLaunchRequestState,
)

if TYPE_CHECKING:
    from collections.abc import Mapping


DAST_CAPABILITY_REVISION_MISMATCH = "CAPABILITY_REVISION_MISMATCH"
_ERR_EXECUTION_TYPE = "DAST launch adapter received a different execution type."
_ERR_REQUEST = "DAST launch request is unavailable in the authorized organization."
_ERR_BINDING = (
    "DAST launch request requires its project's binding and, exactly when the target needs a source "
    "repository, a trigger version from that project."
)
_ERR_SNAPSHOT = "DAST launch request contains an invalid immutable provider snapshot."
_ERR_CAPABILITY_MISMATCH = "DAST provider capability revision changed; synchronize the catalog before retrying."


def build_dast_coalesce_key(
    *,
    project_id: int,
    binding_id: int,
    integration_id: int,
    trigger_project_version_id: int | None,
    params_snapshot: Mapping[str, object],
    capability_snapshot: Mapping[str, object],
) -> str:
    """
    Build the trusted identity for equivalent launches of one DAST binding.

    ``trigger_project_version_id`` is None for a target that declares no repository trigger;
    it stays part of the identity so a sourceless launch never collides with a source-based one.
    """
    return canonical_coalesce_key(
        execution_type=PipelineExecutionKind.DAST,
        project_id=project_id,
        executor_identity={
            "binding_id": binding_id,
            "integration_id": integration_id,
            "trigger_project_version_id": trigger_project_version_id,
        },
        params_snapshot=params_snapshot,
        capability_snapshot=capability_snapshot,
    )


def _mark_capability_resync_required(integration_id: int) -> None:
    with transaction.atomic():
        state = DastIntegrationState.objects.select_for_update().filter(
            integration_id=integration_id,
        ).first()
        if state is None or state.sync_error_code == DAST_CAPABILITY_REVISION_MISMATCH:
            return
        state.sync_error_code = DAST_CAPABILITY_REVISION_MISMATCH
        state.save(update_fields=["sync_error_code", "updated"])


class DastPipelineLaunchAdapter:
    execution_type = PipelineExecutionKind.DAST
    cancellation_mode = ExecutionCancellationMode.COOPERATIVE
    metric_descriptor = ExecutionMetricDescriptor(
        label="dast",
        operations=frozenset({
            ProviderOperation.EXECUTE,
            ProviderOperation.CANCEL,
            ProviderOperation.RESUME,
        }),
    )

    @staticmethod
    def initialize_pipeline(pipeline) -> None:
        DastExecutionState.objects.create(pipeline=pipeline)

    @staticmethod
    def allows_duplicate_delivery(*, pipeline_id: str, task_id: str | None, retries: int) -> bool:
        if retries < 1 or task_id is None:
            return False
        return DastExecutionState.objects.filter(
            pipeline_id=pipeline_id,
            pipeline__run_task_id=str(task_id),
            pipeline__launch_request__state=PipelineLaunchRequestState.DISPATCHED,
            outcome__in=[
                DastExecutionOutcome.STOP_PENDING,
                DastExecutionOutcome.UNREACHABLE,
            ],
        ).exists()

    @staticmethod
    def should_recover(pipeline) -> bool:
        """Resume provider work until it reports an explicit terminal domain state."""
        return DastExecutionState.objects.filter(
            pipeline=pipeline,
            outcome__in=[
                DastExecutionOutcome.STOP_PENDING,
                DastExecutionOutcome.UNREACHABLE,
            ],
        ).exists()

    @staticmethod
    def invoke(runtime, pipeline_id: str):
        return runtime.run_dast(pipeline_id)

    def build_plan(self, context: LaunchPlanningContext) -> ExecutionPlan:
        if context.execution_type != self.execution_type:
            raise ExecutionPlanError(_ERR_EXECUTION_TYPE)
        try:
            request = (
                PipelineLaunchRequest.objects
                .select_related(
                    "dast_binding__target__integration__dast_state",
                    "dast_binding__target__integration__vpn_integration__vpn_secret",
                    "project__product__prod_type",
                    "trigger_project_version",
                )
                .get(
                    pk=context.launch_request_id,
                    project_id=context.project_id,
                    execution_type=PipelineExecutionKind.DAST.value,
                    project__product__prod_type__aist_organization__id=context.authority.organization_id,
                )
            )
        except PipelineLaunchRequest.DoesNotExist as exc:
            raise ExecutionPlanError(_ERR_REQUEST) from exc
        binding = request.dast_binding
        if binding is None:
            raise ExecutionPlanError(_ERR_BINDING)
        readiness = check_dast_binding_readiness(binding)
        if not readiness.ready:
            codes = ", ".join(issue.code.value for issue in readiness.issues)
            message = f"DAST launch is not ready: {codes}."
            raise ExecutionPlanError(message)
        trigger_version = request.trigger_project_version
        if binding.launch_violations(project_id=request.project_id, trigger_version=trigger_version):
            raise ExecutionPlanError(_ERR_BINDING)

        try:
            frozen_target = DastTargetSnapshot.from_snapshot(request.get_snapshots().capability_snapshot())
            DastBindingParameters.from_snapshot(request.get_snapshots().params_snapshot(), target=frozen_target)
            current_target = binding.target.get_snapshot()
        except DastConfigError as exc:
            raise ExecutionPlanError(_ERR_SNAPSHOT) from exc
        if frozen_target.revision_identity != current_target.revision_identity:
            _mark_capability_resync_required(binding.target.integration_id)
            raise ExecutionPlanError(_ERR_CAPABILITY_MISMATCH)

        coalesce_key = request.coalesce_key
        if not coalesce_key:
            detail = "DAST launch request is missing its frozen execution identity."
            raise ExecutionPlanError(detail)
        launch_data = PipelineLaunchData(request.get_initial_launch_data_snapshot())
        launch_data.dast_execution = DastExecutionSnapshot(
            integration_id=binding.target.integration_id,
            source_repo_key=binding.source_repo_key,
        )
        initial_launch_data = launch_data.as_dict()
        return ExecutionPlan(
            execution_type=self.execution_type,
            task_name=PipelineTaskName.RUN_PIPELINE_EXECUTION,
            task_args=(),
            project_id=request.project_id,
            trigger_project_version_id=trigger_version.pk if trigger_version is not None else None,
            effective_version_policy=(
                EffectiveVersionPolicy.RESOLVE_FROM_EXECUTION_RESULT
                if binding.requires_source_repository
                else EffectiveVersionPolicy.NO_VERSION
            ),
            effective_project_version_id=None,
            resource_key=f"dast-integration:{binding.target.integration_id}",
            resource_limit=1,
            coalesce_key=coalesce_key,
            initial_launch_data=initial_launch_data,
            authority=context.authority,
        )
