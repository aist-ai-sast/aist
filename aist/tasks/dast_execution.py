"""One DAST execution attempt, from runtime preparation to its persisted outcome."""

from __future__ import annotations

import functools
import logging
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from celery.utils.time import get_exponential_backoff_interval
from django.db import transaction
from django.utils import timezone

from aist.execution.dast_trigger import DastTrigger
from aist.execution.observability import (
    observe_dast_finalization,
    observe_dast_outcome,
    observe_provider_call,
)
from aist.integrations.dast_config import DastBindingParameters, DastTargetSnapshot
from aist.integrations.dast_readiness import check_dast_binding_readiness
from aist.integrations.dast_report import (
    DAST_RESULT_MAX_BYTES,
    DastReportValidationError,
    validate_dast_report_bytes,
)
from aist.launch_data import PipelineLaunchData
from aist.logging_transport import install_pipeline_logging
from aist.models import (
    AISTPipeline,
    DastExecutionOutcome,
    DastExecutionState,
    PipelineExecutionType,
    PipelineLaunchRequest,
    PipelineLaunchRequestState,
)
from aist.models.organizations import resolve_bound_vpn
from aist.pipeline_args import DastPipelineArguments, PipelineArguments
from aist.services.dast_finalization import DastFinalizationError, finalize_dast_report
from aist.services.dast_outcomes import (
    ClassifiedDastOutcome,
    DastPipelineOutcomeCode,
    classify_dast_execution_result,
    record_dast_pipeline_outcome,
)
from aist.utils.pipeline import (
    finish_pipeline,
)
from aist.utils.pipeline_imports import _import_sast_pipeline_package
from aist.utils.vpn import vpn_sidecar_context

if TYPE_CHECKING:
    from aist.execution.contracts import ProviderOperation
    from aist.integrations.resolver import ResolvedIntegration

# --------------------------------------------------------------------
# Ensure external "pipeline" package is importable before importing it
# --------------------------------------------------------------------
_import_sast_pipeline_package()

from pipeline.dast import (  # type: ignore[import-not-found]  # noqa: E402
    DastConnectorOutcomeState,
    DastExecutionIncomplete,
    DastExecutionInput,
    DastExecutionLocalFailure,
    DastRecoveryState,
    DastStartCommand,
)
from pipeline.dast.contracts import CONTRACT_VERSION  # type: ignore[import-not-found]  # noqa: E402
from pipeline.execution import execute_pipeline  # type: ignore[import-not-found]  # noqa: E402

# -------------------------
# Error messages/constants
# -------------------------


_ERR_DAST_RUNTIME = "Persisted DAST execution data is invalid."
_RETRY_BASE_SECONDS = 15
_RETRY_MAX_SECONDS = 300
_FAILURE_OUTCOMES: tuple[tuple[type[Exception], DastPipelineOutcomeCode], ...] = (
    (DastExecutionLocalFailure, DastPipelineOutcomeCode.RUNTIME_FAILED),
    (DastReportValidationError, DastPipelineOutcomeCode.INVALID_RESULT),
    (DastFinalizationError, DastPipelineOutcomeCode.INVALID_RESULT),
)


@dataclass(frozen=True, slots=True)
class _DastRuntimeSpec:
    gateway_url: str
    command: DastStartCommand
    token: str = field(repr=False)
    ca_bundle: str = field(repr=False)
    vpn: ResolvedIntegration | None = field(repr=False)
    recovery: DastRecoveryState
    arguments: PipelineArguments
    allowed_repository_keys: frozenset[str]
    stop_requested: bool
    binding: object
    lead: object | None


def _prepare_dast_runtime(pipeline_id: str) -> _DastRuntimeSpec:
    with transaction.atomic():
        pipeline = (
            AISTPipeline.objects
            .select_for_update(of=("self",))
            .select_related(
                "project__product__prod_type",
                "trigger_project_version",
                "dast_execution_state",
                "launch_request__dast_binding__target__integration__vpn_integration",
                "launch_request__requester",
            )
            .get(pk=pipeline_id, execution_type=PipelineExecutionType.DAST)
        )
        launch_request = pipeline.launch_request
        execution_state = pipeline.dast_execution_state
        binding = launch_request.dast_binding
        if binding is None or binding.target.integration.organization_id != pipeline.project.organization_id:
            raise ValueError(_ERR_DAST_RUNTIME)
        if not check_dast_binding_readiness(binding).ready:
            raise ValueError(_ERR_DAST_RUNTIME)
        if binding.launch_violations(
            project_id=pipeline.project_id,
            trigger_version=pipeline.trigger_project_version,
        ):
            raise ValueError(_ERR_DAST_RUNTIME)
        snapshots = launch_request.get_snapshots()
        capability = DastTargetSnapshot.from_snapshot(snapshots.capability_snapshot())
        parameters = DastBindingParameters.from_snapshot(snapshots.params_snapshot(), target=capability)
        PipelineLaunchData(pipeline.launch_data).dast_execution.verify_against(binding, capability=capability)
        trigger = (
            DastTrigger.from_project_version(
                pipeline.trigger_project_version,
                repository_key=binding.source_repo_key,
            )
            if binding.requires_source_repository
            else None
        )
        integration = binding.target.integration
        config = integration.get_dast_config()
        token = (integration.secret or "").strip()
        if not token:
            raise ValueError(_ERR_DAST_RUNTIME)

        updated_fields = ["updated"]
        # This timestamp is observability only. Silence never changes the run lifecycle.
        if execution_state.last_progress_at is None:
            execution_state.last_progress_at = timezone.now()
            updated_fields.append("last_progress_at")
        execution_state.outcome = DastExecutionOutcome.RUNNING
        updated_fields.append("outcome")
        execution_state.save(update_fields=updated_fields)
        recovery = DastRecoveryState(
            correlation_id=pipeline.id,
            idempotency_key=str(launch_request.task_id),
            run_id=execution_state.run_id,
            log_cursor=execution_state.log_cursor,
        )
        arguments = PipelineArguments(
            project=pipeline.project,
            payload=DastPipelineArguments(
                project=pipeline.project,
                binding=binding,
                trigger_project_version=pipeline.trigger_project_version,
                parameters=parameters.to_snapshot(),
                capability=capability.to_snapshot(),
            ),
        )
        return _DastRuntimeSpec(
            gateway_url=config.gateway_url,
            command=DastStartCommand.from_wire({
                "contract_version": CONTRACT_VERSION,
                "idempotency_key": str(launch_request.task_id),
                "correlation_id": pipeline.id,
                "target_id": capability.provider_id,
                "capability_revision": capability.capability_revision,
                "trigger": trigger.to_wire() if trigger is not None else None,
                "parameters": parameters.to_snapshot(),
            }),
            token=token,
            ca_bundle=config.ca_bundle,
            vpn=resolve_bound_vpn(integration.vpn_integration, organization_id=integration.organization_id),
            recovery=recovery,
            arguments=arguments,
            allowed_repository_keys=frozenset(capability.repository_keys),
            stop_requested=execution_state.cancel_requested_at is not None,
            binding=binding,
            lead=launch_request.requester,
        )


def _apply_dast_recovery(execution_state, recovery) -> list[str]:
    """
    Write what the connector reported onto this run's durable state.

    The one writer of a recovery report's run id and log cursor, both while the run is going and when it
    ends. ``last_progress_at`` advances only when the run id changes or new log events arrive.

    Returns the fields to save.
    """
    delivered_progress = (
        (recovery.run_id or "") != (execution_state.run_id or "")
        or recovery.log_cursor > execution_state.log_cursor
    )
    execution_state.run_id = recovery.run_id
    execution_state.log_cursor = recovery.log_cursor
    update_fields = ["run_id", "log_cursor", "updated"]
    if delivered_progress:
        execution_state.last_progress_at = timezone.now()
        update_fields.append("last_progress_at")
    return update_fields


def _record_dast_progress(pipeline_id: str, recovery) -> None:
    """
    Record the connector's progress while its run is still going.

    Persists the provider's run id while the run lasts, so the queue surface can address the run before the
    execution returns. The executor owns what happens if this fails.
    """
    with transaction.atomic():
        execution_state = (
            DastExecutionState.objects.select_for_update().filter(pipeline_id=pipeline_id).first()
        )
        if execution_state is None:
            return
        execution_state.save(update_fields=_apply_dast_recovery(execution_state, recovery))


def _persist_dast_execution_result(pipeline_id: str, result) -> None:
    outcome_map = {
        DastConnectorOutcomeState.TERMINAL: DastExecutionOutcome.TERMINAL,
        DastConnectorOutcomeState.STOP_PENDING: DastExecutionOutcome.STOP_PENDING,
        DastConnectorOutcomeState.CANCELLED_BEFORE_START: DastExecutionOutcome.CANCELLED_BEFORE_START,
        DastConnectorOutcomeState.UNREACHABLE: DastExecutionOutcome.UNREACHABLE,
    }
    with transaction.atomic():
        pipeline = AISTPipeline.objects.select_for_update().get(pk=pipeline_id)
        execution_state = DastExecutionState.objects.select_for_update().get(pipeline=pipeline)
        if result.recovery.correlation_id != pipeline.id:
            raise ValueError(_ERR_DAST_RUNTIME)
        update_fields = _apply_dast_recovery(execution_state, result.recovery)
        execution_state.outcome = outcome_map[result.outcome.state]
        update_fields.append("outcome")
        execution_state.save(update_fields=update_fields)
        report_path = getattr(result, "report_path", None)
        if report_path is not None:
            launch_data = PipelineLaunchData(pipeline.launch_data)
            launch_data.merge({"output_dir": str(report_path.parent)})
            pipeline.launch_data = launch_data.as_dict()
            pipeline.save(update_fields=["launch_data", "updated"])


def _execute_dast_pipeline(pipeline_id: str, logger=None):
    logger = logger or logging.getLogger(__name__)
    runtime = _prepare_dast_runtime(pipeline_id)
    workspace, output_dir = runtime.arguments.prepare_execution(pipeline_id)
    try:
        workspace.chmod(0o700)
        token_file = workspace / "token"
        token_file.write_text(runtime.token, encoding="utf-8")
        token_file.chmod(0o600)
        ca_file = None
        if runtime.ca_bundle:
            ca_file = workspace / "ca.pem"
            ca_file.write_text(runtime.ca_bundle, encoding="utf-8")
            ca_file.chmod(0o600)
        with vpn_sidecar_context(runtime.vpn, execution_id=pipeline_id) as (vpn_container, _vpn_proxy):
            execution = DastExecutionInput(
                pipeline_id=pipeline_id,
                gateway_url=runtime.gateway_url,
                command=runtime.command,
                workspace=workspace / "connector",
                output_dir=output_dir,
                token_file=token_file,
                ca_file=ca_file,
                vpn_container_name=vpn_container,
                recovery=runtime.recovery,
                stop_requested=runtime.stop_requested,
                on_progress=functools.partial(_record_dast_progress, pipeline_id),
            )
            result = execute_pipeline(PipelineExecutionType.DAST, execution)
    finally:
        runtime.arguments.cleanup_workspace(pipeline_id)
    _persist_dast_execution_result(pipeline_id, result)
    terminal_result = getattr(result, "terminal_result", None)
    if terminal_result is not None and terminal_result.status.carries_report:
        if result.recovery.run_id is None:
            raise ValueError(_ERR_DAST_RUNTIME)
        report_path = getattr(result, "report_path", None)
        if report_path is None:
            raise ValueError(_ERR_DAST_RUNTIME)
        with report_path.open("rb") as report_file:
            raw_report = report_file.read(DAST_RESULT_MAX_BYTES + 1)
        validated_report = validate_dast_report_bytes(
            raw_report,
            target_id=runtime.command.target_id,
            allowed_repository_keys=runtime.allowed_repository_keys,
            expected_run_id=result.recovery.run_id,
        )
        finalize_dast_report(
            pipeline_id=pipeline_id,
            report=validated_report,
            binding=runtime.binding,
            logger=logger,
            lead=runtime.lead,
        )
        observe_dast_finalization(succeeded=True)
    return result


def _mark_dast_unreachable(pipeline_id: str) -> None:
    DastExecutionState.objects.filter(pipeline_id=pipeline_id).update(
        outcome=DastExecutionOutcome.UNREACHABLE,
    )


def _finish_dast_cancellation(pipeline_id: str) -> None:
    with transaction.atomic():
        launch_request = (
            PipelineLaunchRequest.objects
            .select_for_update()
            .get(pipeline_id=pipeline_id)
        )
        launch_request.state = PipelineLaunchRequestState.CANCELLED
        launch_request.save(update_fields=["state", "updated"])
    finish_pipeline(pipeline_id, degraded=True)


def _retry_dast_execution(
    task,
    pipeline_id: str,
    *,
    exc: Exception | None = None,
    logger=None,
) -> None:
    countdown = get_exponential_backoff_interval(
        factor=_RETRY_BASE_SECONDS,
        retries=int(task.request.retries),
        maximum=_RETRY_MAX_SECONDS,
        full_jitter=False,
    )
    raise task.retry(exc=exc, countdown=countdown)


def _observe_dast_result(result, *, outcome: str) -> None:
    terminal_result = result.terminal_result
    selection = terminal_result.selection if terminal_result is not None else {}
    observe_dast_outcome(
        outcome=outcome,
        logs_delivered=result.telemetry.logs_delivered,
        log_lag_seconds=result.telemetry.max_log_lag_seconds,
        relation=selection.get("relation"),
        distance=selection.get("distance"),
    )


def _record_dast_terminal_outcome(pipeline_id: str, result, outcome: ClassifiedDastOutcome) -> None:
    """
    Record one settled connector result and take the pipeline to a terminal status.

    A result that carries a report was handed to the findings tail by ``finalize_dast_report``, which finishes it.
    """
    record_dast_pipeline_outcome(pipeline_id, outcome.code)
    terminal_result = result.terminal_result
    if terminal_result is not None and terminal_result.status.carries_report:
        return
    if outcome.code is DastPipelineOutcomeCode.CANCELLED:
        _finish_dast_cancellation(pipeline_id)
    elif outcome.degraded:
        finish_pipeline(pipeline_id, degraded=True)


def _handle_dast_execution_result(task, pipeline_id: str, result, logger=None):
    outcome = classify_dast_execution_result(result)
    if outcome is None:
        _observe_dast_result(result, outcome="pending")
        return _retry_dast_execution(task, pipeline_id, logger=logger)
    _observe_dast_result(result, outcome=outcome.code.value)
    _record_dast_terminal_outcome(pipeline_id, result, outcome)
    return None


def _failure_outcome(exc: Exception) -> DastPipelineOutcomeCode:
    for failure_type, code in _FAILURE_OUTCOMES:
        if isinstance(exc, failure_type):
            return code
    return DastPipelineOutcomeCode.PROVIDER_FAILED


def run_dast_execution(task, pipeline_id: str, *, operation: ProviderOperation):
    """
    Run one DAST attempt and map every failure mode onto a persisted pipeline outcome.

    Shared by the first dispatch and by reconciliation-driven resumes: both re-enter the same
    executor with the persisted run id and log cursor, so the only difference between them is
    which provider operation the metrics are attributed to.
    """
    logger = install_pipeline_logging(pipeline_id, "INFO")
    started_at = time.monotonic()
    try:
        result = _execute_dast_pipeline(pipeline_id, logger)
    except DastExecutionIncomplete as exc:
        observe_provider_call(
            operation=operation,
            duration_seconds=time.monotonic() - started_at,
            error_code=DastExecutionOutcome.UNREACHABLE,
        )
        _mark_dast_unreachable(pipeline_id)
        return _retry_dast_execution(task, pipeline_id, exc=exc, logger=logger)
    except Exception as exc:
        code = _failure_outcome(exc)
        observe_provider_call(
            operation=operation,
            duration_seconds=time.monotonic() - started_at,
            error_code=code,
        )
        if code is DastPipelineOutcomeCode.INVALID_RESULT:
            observe_dast_finalization(succeeded=False)
        logger.exception(
            "DAST execution failed with %s (pipeline_id=%s operation=%s)", code, pipeline_id, operation,
        )
        record_dast_pipeline_outcome(pipeline_id, code)
        finish_pipeline(pipeline_id, degraded=True)
        if code is DastPipelineOutcomeCode.PROVIDER_FAILED:
            raise
        return None
    observe_provider_call(operation=operation, duration_seconds=time.monotonic() - started_at)
    return _handle_dast_execution_result(task, pipeline_id, result, logger)
