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
from aist.execution.dast_deadlines import dast_execution_over, dast_final_pass_taken
from aist.integrations.dast_config import DastBindingParameters, DastConfigError, DastTargetSnapshot
from aist.integrations.dast_readiness import check_dast_binding_readiness
from aist.models import (
    AISTProjectVersion,
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
_ERR_BINDING = "DAST launch request requires its authorized project binding."
_ERR_TRIGGER = "DAST launch request requires a trigger version from the same project."
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
            ProviderOperation.HARVEST,
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
        """
        Resume unfinished work, and grant a run that is over exactly one closing pass.

        Resuming is expensive (VPN tunnel, image pull), so a run that is over is not restarted on
        every pass -- but one closing pass is what asks the provider for a result when the worker
        that would have asked is gone. ``dast_execution_over`` is shared with the retry path.
        """
        state = DastExecutionState.objects.filter(
            pipeline=pipeline,
            outcome__in=[
                DastExecutionOutcome.STOP_PENDING,
                DastExecutionOutcome.UNREACHABLE,
            ],
        ).values("deadline", "last_progress_at", "recovery_checkpoint").first()
        if state is None:
            return False
        if not dast_execution_over(
            deadline=state["deadline"],
            last_progress_at=state["last_progress_at"],
        ):
            return True
        return not dast_final_pass_taken(state["recovery_checkpoint"])

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
        if binding is None or binding.project_id != request.project_id:
            raise ExecutionPlanError(_ERR_BINDING)
        readiness = check_dast_binding_readiness(binding)
        if not readiness.ready:
            codes = ", ".join(issue.code.value for issue in readiness.issues)
            message = f"DAST launch is not ready: {codes}."
            raise ExecutionPlanError(message)
        trigger_version = None
        if binding.requires_source_repository:
            try:
                trigger_version = AISTProjectVersion.objects.get(
                    pk=request.trigger_project_version_id,
                    project_id=request.project_id,
                    project__product__prod_type__aist_organization__id=context.authority.organization_id,
                )
            except (AISTProjectVersion.DoesNotExist, TypeError, ValueError) as exc:
                raise ExecutionPlanError(_ERR_TRIGGER) from exc
        elif request.trigger_project_version_id is not None:
            raise ExecutionPlanError(_ERR_TRIGGER)

        try:
            frozen_target = DastTargetSnapshot.from_snapshot(request.get_snapshots().capability_snapshot())
            frozen_parameters = DastBindingParameters.from_snapshot(
                request.get_snapshots().params_snapshot(),
                target=frozen_target,
            )
            current_target = binding.target.get_snapshot()
        except DastConfigError as exc:
            raise ExecutionPlanError(_ERR_SNAPSHOT) from exc
        frozen_revision = (
            frozen_target.provider_id,
            frozen_target.contract_revision,
            frozen_target.capability_revision,
            frozen_target.schema_digest,
        )
        current_revision = (
            current_target.provider_id,
            current_target.contract_revision,
            current_target.capability_revision,
            current_target.schema_digest,
        )
        if frozen_revision != current_revision:
            _mark_capability_resync_required(binding.target.integration_id)
            raise ExecutionPlanError(_ERR_CAPABILITY_MISMATCH)

        params_snapshot = frozen_parameters.to_snapshot()
        capability_snapshot = frozen_target.to_snapshot()
        coalesce_key = request.coalesce_key
        if not coalesce_key:
            detail = "DAST launch request is missing its frozen execution identity."
            raise ExecutionPlanError(detail)
        initial_launch_data = request.get_initial_launch_data_snapshot()
        initial_launch_data["dast_execution"] = {
            "binding_id": binding.pk,
            "integration_id": binding.target.integration_id,
            "target_id": frozen_target.provider_id,
            "source_repo_key": binding.source_repo_key,
            "parameters": params_snapshot,
            "capability": capability_snapshot,
        }
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
