from __future__ import annotations

from typing import TYPE_CHECKING

from django.db import transaction

from aist.execution.coalescing import canonical_coalesce_key
from aist.execution.contracts import (
    EffectiveVersionPolicy,
    ExecutionPlan,
    ExecutionPlanError,
    LaunchPlanningContext,
    PipelineExecutionKind,
    PipelineTaskName,
)
from aist.integrations.dast_config import DastBindingParameters, DastConfigError, DastTargetSnapshot
from aist.integrations.dast_readiness import check_dast_binding_readiness
from aist.models import AISTProjectVersion, DastIntegrationState, PipelineLaunchRequest

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
    params_snapshot: Mapping[str, object],
    capability_snapshot: Mapping[str, object],
) -> str:
    """Build the trusted identity for equivalent launches of one DAST binding."""
    return canonical_coalesce_key(
        execution_type=PipelineExecutionKind.DAST,
        project_id=project_id,
        executor_identity={
            "binding_id": binding_id,
            "integration_id": integration_id,
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
        try:
            trigger_version = AISTProjectVersion.objects.get(
                pk=request.trigger_project_version_id,
                project_id=request.project_id,
                project__product__prod_type__aist_organization__id=context.authority.organization_id,
            )
        except (AISTProjectVersion.DoesNotExist, TypeError, ValueError) as exc:
            raise ExecutionPlanError(_ERR_TRIGGER) from exc

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
        coalesce_key = build_dast_coalesce_key(
            project_id=request.project_id,
            binding_id=binding.pk,
            integration_id=binding.target.integration_id,
            params_snapshot=params_snapshot,
            capability_snapshot=capability_snapshot,
        )
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
            trigger_project_version_id=trigger_version.pk,
            effective_version_policy=EffectiveVersionPolicy.RESOLVE_FROM_EXECUTION_RESULT,
            effective_project_version_id=None,
            resource_key=f"dast-integration:{binding.target.integration_id}",
            resource_limit=1,
            coalesce_key=coalesce_key,
            initial_launch_data=initial_launch_data,
            authority=context.authority,
        )
