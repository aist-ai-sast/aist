from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

from celery import shared_task
from django.db import transaction
from django.utils import timezone
from dojo.models import Finding, Test

from aist.celery_signals import _update_action_run
from aist.execution.adapters import UnknownLaunchAdapterError
from aist.execution.dast_trigger import DastTrigger
from aist.execution.dispatching import LaunchAcceptance, accept_published_launch
from aist.execution.observability import (
    observe_dast_finalization,
    observe_dast_outcome,
    observe_provider_call,
)
from aist.execution.registry import execution_driver_registry
from aist.integrations.claude import claude_auth_env
from aist.integrations.dast_config import DastBindingParameters, DastTargetSnapshot
from aist.integrations.dast_readiness import check_dast_binding_readiness
from aist.integrations.dast_report import (
    DastReportExpectations,
    DastReportValidationError,
    validate_dast_terminal_result_bytes,
)
from aist.integrations.resolver import ResolvedIntegration, resolve_integration
from aist.launch_data import PipelineLaunchData
from aist.logging_transport import install_pipeline_logging
from aist.models import (
    AISTPipeline,
    AISTProjectVersion,
    AISTStatus,
    DastExecutionOutcome,
    DastExecutionState,
    OrgIntegrationType,
    PipelineExecutionType,
    PipelineLaunchRequest,
    PipelineLaunchRequestState,
)
from aist.pipeline_args import PipelineArguments
from aist.services.dast_finalization import DastFinalizationError, finalize_dast_report
from aist.services.dast_outcomes import (
    DastPipelineOutcomeCode,
    classify_dast_execution_result,
    record_dast_pipeline_outcome,
)
from aist.services.pipeline_results import (
    attach_findings_to_project_version,
    schedule_pipeline_postprocessing,
)
from aist.tasks.dedup import watch_deduplication
from aist.utils.agent_runtime import build_agent_runtime_env
from aist.utils.analyzer_outcomes import consume_analyzer_outcomes
from aist.utils.bridge_client_factory import build_bridge_client_from_settings
from aist.utils.pipeline import (
    cleanup_terminal_project_build_paths,
    finish_pipeline,
    get_project_build_path,
    set_pipeline_status,
)
from aist.utils.pipeline_imports import _import_sast_pipeline_package
from aist.utils.vpn import vpn_sidecar_context

# --------------------------------------------------------------------
# Ensure external "pipeline" package is importable before importing it
# --------------------------------------------------------------------
_import_sast_pipeline_package()

from celery.exceptions import Ignore  # noqa: E402
from pipeline.config_utils import AnalyzersConfigHelper  # type: ignore[import-not-found]  # noqa: E402
from pipeline.dast import (  # type: ignore[import-not-found]  # noqa: E402
    DastConnectorOutcomeState,
    DastExecutionIncomplete,
    DastExecutionInput,
    DastRecoveryState,
    DastStartCommand,
)
from pipeline.execution import execute_pipeline  # type: ignore[import-not-found]  # noqa: E402
from pipeline.sast_execution import SastExecutionInput  # type: ignore[import-not-found]  # noqa: E402

from aist.internal_upload import upload_results_internal  # noqa: E402

# -------------------------
# Error messages/constants
# -------------------------
MSG_PROJECT_BUILD_PATH_NOT_SET = "Project build path for AIST is not setup"
_ERR_DAST_RUNTIME = "Persisted DAST execution data is invalid."
_DAST_EXECUTION_TIMEOUT = timedelta(hours=4)
_DAST_UNREACHABLE_GRACE = timedelta(minutes=15)


@dataclass(frozen=True, slots=True)
class _DastRuntimeSpec:
    gateway_url: str
    command: DastStartCommand
    token: str = field(repr=False)
    ca_bundle: str = field(repr=False)
    vpn_integration: object | None = field(repr=False)
    recovery: DastRecoveryState
    allowed_repository_keys: frozenset[str]
    deadline_at: datetime
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
        if (
            binding is None
            or binding.project_id != pipeline.project_id
            or pipeline.trigger_project_version_id is None
            or pipeline.trigger_project_version.project_id != pipeline.project_id
            or binding.target.integration.organization_id != pipeline.project.organization_id
        ):
            raise ValueError(_ERR_DAST_RUNTIME)
        if not check_dast_binding_readiness(binding).ready:
            raise ValueError(_ERR_DAST_RUNTIME)
        execution_snapshot = pipeline.launch_data.get("dast_execution")
        if not isinstance(execution_snapshot, dict):
            raise TypeError(_ERR_DAST_RUNTIME)
        if (
            execution_snapshot.get("binding_id") != binding.pk
            or execution_snapshot.get("integration_id") != binding.target.integration_id
            or execution_snapshot.get("source_repo_key") != binding.source_repo_key
        ):
            raise ValueError(_ERR_DAST_RUNTIME)
        capability = DastTargetSnapshot.from_snapshot(execution_snapshot.get("capability"))
        parameters = DastBindingParameters.from_snapshot(
            execution_snapshot.get("parameters"),
            target=capability,
        )
        current_capability = binding.target.get_snapshot()
        if (
            capability.provider_id != execution_snapshot.get("target_id")
            or capability.provider_id != current_capability.provider_id
            or capability.contract_revision != current_capability.contract_revision
            or capability.capability_revision != current_capability.capability_revision
            or capability.schema_digest != current_capability.schema_digest
        ):
            raise ValueError(_ERR_DAST_RUNTIME)
        trigger = DastTrigger.from_project_version(
            pipeline.trigger_project_version,
            repository_key=binding.source_repo_key,
        )
        integration = binding.target.integration
        config = integration.get_dast_config()
        token = (integration.secret or "").strip()
        if not token:
            raise ValueError(_ERR_DAST_RUNTIME)

        now = timezone.now()
        if execution_state.deadline is None:
            execution_state.deadline = now + _DAST_EXECUTION_TIMEOUT
        execution_state.outcome = DastExecutionOutcome.RUNNING
        execution_state.save(update_fields=["deadline", "outcome", "updated"])
        recovery = DastRecoveryState(
            correlation_id=pipeline.id,
            idempotency_key=str(launch_request.task_id),
            run_id=execution_state.run_id,
            log_cursor=execution_state.log_cursor,
        )
        return _DastRuntimeSpec(
            gateway_url=config.gateway_url,
            command=DastStartCommand.from_wire({
                "contract_version": "2.0",
                "idempotency_key": str(launch_request.task_id),
                "correlation_id": pipeline.id,
                "target_id": capability.provider_id,
                "capability_revision": capability.capability_revision,
                "trigger": trigger.to_wire(),
                "parameters": parameters.to_snapshot(),
            }),
            token=token,
            ca_bundle=config.ca_bundle,
            vpn_integration=integration.vpn_integration,
            recovery=recovery,
            allowed_repository_keys=frozenset(capability.repository_keys),
            deadline_at=execution_state.deadline,
            stop_requested=execution_state.cancel_requested_at is not None,
            binding=binding,
            lead=launch_request.requester,
        )


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
        execution_state.run_id = result.recovery.run_id
        execution_state.log_cursor = result.recovery.log_cursor
        execution_state.outcome = outcome_map[result.outcome.state]
        execution_state.save(update_fields=["run_id", "log_cursor", "outcome", "updated"])


def _execute_dast_pipeline(pipeline_id: str, logger=None):
    logger = logger or logging.getLogger(__name__)
    runtime = _prepare_dast_runtime(pipeline_id)
    vpn_resolved = (
        ResolvedIntegration(
            integration=runtime.vpn_integration,
            config=dict(runtime.vpn_integration.config or {}),
        )
        if runtime.vpn_integration is not None
        else None
    )
    with TemporaryDirectory(prefix=f"aist-dast-{pipeline_id}-") as temporary_directory:
        workspace = Path(temporary_directory)
        token_file = workspace / "token"
        token_file.write_text(runtime.token, encoding="utf-8")
        token_file.chmod(0o600)
        ca_file = None
        if runtime.ca_bundle:
            ca_file = workspace / "ca.pem"
            ca_file.write_text(runtime.ca_bundle, encoding="utf-8")
            ca_file.chmod(0o600)
        with vpn_sidecar_context(vpn_resolved, execution_id=pipeline_id) as (vpn_container, _vpn_proxy):
            execution = DastExecutionInput(
                pipeline_id=pipeline_id,
                gateway_url=runtime.gateway_url,
                command=runtime.command,
                workspace=workspace / "connector",
                token_file=token_file,
                ca_file=ca_file,
                vpn_container_name=vpn_container,
                recovery=runtime.recovery,
                deadline_at=runtime.deadline_at,
                stop_requested=runtime.stop_requested,
            )
            result = execute_pipeline(PipelineExecutionType.DAST, execution)
    terminal_result = getattr(result, "terminal_result", None)
    if terminal_result is not None and terminal_result.status.value == "succeeded":
        if result.recovery.run_id is None:
            raise ValueError(_ERR_DAST_RUNTIME)
        validated_report = validate_dast_terminal_result_bytes(
            json.dumps(
                terminal_result.to_wire(),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8"),
            expectations=DastReportExpectations(
                correlation_id=runtime.command.correlation_id,
                run_id=result.recovery.run_id,
                target_id=runtime.command.target_id,
                allowed_repository_keys=runtime.allowed_repository_keys,
            ),
        )
        finalize_dast_report(
            pipeline_id=pipeline_id,
            report=validated_report,
            binding=runtime.binding,
            logger=logger,
            lead=runtime.lead,
        )
        observe_dast_finalization(succeeded=True)
    _persist_dast_execution_result(pipeline_id, result)
    return result


def _dast_reconciliation_exhausted(pipeline_id: str) -> bool:
    deadline = DastExecutionState.objects.filter(pipeline_id=pipeline_id).values_list(
        "deadline",
        flat=True,
    ).first()
    return deadline is not None and timezone.now() >= deadline + _DAST_UNREACHABLE_GRACE


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


def _retry_dast_execution(task, pipeline_id: str, *, exc: Exception | None = None) -> None:
    if _dast_reconciliation_exhausted(pipeline_id):
        _mark_dast_unreachable(pipeline_id)
        record_dast_pipeline_outcome(pipeline_id, DastPipelineOutcomeCode.TIMEOUT)
        finish_pipeline(pipeline_id, degraded=True)
        return
    countdown = min(15 * (2 ** min(int(task.request.retries), 5)), 300)
    raise task.retry(exc=exc, countdown=countdown, max_retries=None)


def _handle_dast_execution_result(task, pipeline_id: str, result):
    terminal_result = result.terminal_result
    selection = terminal_result.selection if terminal_result is not None else {}
    if result.outcome.state in {
        DastConnectorOutcomeState.STOP_PENDING,
        DastConnectorOutcomeState.UNREACHABLE,
    }:
        observe_dast_outcome(
            outcome="pending",
            logs_delivered=result.telemetry.logs_delivered,
            log_lag_seconds=result.telemetry.max_log_lag_seconds,
            relation=selection.get("relation"),
            distance=selection.get("distance"),
        )
        return _retry_dast_execution(task, pipeline_id)
    outcome = classify_dast_execution_result(result)
    if outcome is None:
        return None
    observe_dast_outcome(
        outcome=outcome.code.value,
        logs_delivered=result.telemetry.logs_delivered,
        log_lag_seconds=result.telemetry.max_log_lag_seconds,
        relation=selection.get("relation"),
        distance=selection.get("distance"),
    )
    record_dast_pipeline_outcome(pipeline_id, outcome.code)
    if result.outcome.state is DastConnectorOutcomeState.CANCELLED_BEFORE_START:
        _finish_dast_cancellation(pipeline_id)
        return None
    if outcome.degraded:
        if result.terminal_result is not None and result.terminal_result.status.value == "stopped":
            _finish_dast_cancellation(pipeline_id)
        else:
            finish_pipeline(pipeline_id, degraded=True)
    return None


def postprocess_findings(pipeline_id: str, log_level: str) -> None:
    """
    Transition pipeline to WAITING_DEDUPLICATION_TO_FINISH and schedule the watcher task.

    The watcher task ID is saved in the same transaction as the status change so that
    any code reading watch_dedup_task_id always sees it alongside the new status.
    The Celery dispatch is deferred to on_commit to avoid dispatching tasks that would
    run against uncommitted state.
    """
    schedule_pipeline_postprocessing(pipeline_id, log_level, dedup_task=watch_deduplication)


def attach_findings_and_finish(
    *,
    pipeline_id: str,
    project_version_id: int | None,
    finding_ids: list[int],
    log_level: str,
    logger,
) -> None:
    """
    Shared tail for every path that produces findings for a pipeline: attach them to the
    project version (and its GIT_HASH parent, if any), then hand off to
    postprocess_findings (dedup) or finish_pipeline directly when there are no findings.

    Used by both the SAST execution worker (after analyzer upload) and import_report (after
    manual report import, any scan_type) — the two pipeline-creation paths converge here
    once findings are attached to a Test, so this logic exists exactly once instead of
    being reimplemented per path.
    """
    finding_ids = attach_findings_to_project_version(
        project_version_id=project_version_id,
        finding_ids=finding_ids,
        logger=logger,
    )

    if not finding_ids:
        logger.info("No findings to enrich; Finishing pipeline")
        finish_pipeline(pipeline_id)
    else:
        postprocess_findings(pipeline_id, log_level)


def _run_dast_execution(task, pipeline_id: str, *, operation: str):
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
            error_code="UNREACHABLE",
        )
        _mark_dast_unreachable(pipeline_id)
        return _retry_dast_execution(task, pipeline_id, exc=exc)
    except (DastReportValidationError, DastFinalizationError):
        observe_provider_call(
            operation=operation,
            duration_seconds=time.monotonic() - started_at,
            error_code="INVALID_RESULT",
        )
        observe_dast_finalization(succeeded=False)
        logger.exception("Invalid DAST result (pipeline_id=%s operation=%s)", pipeline_id, operation)
        record_dast_pipeline_outcome(pipeline_id, DastPipelineOutcomeCode.INVALID_RESULT)
        finish_pipeline(pipeline_id, degraded=True)
        return None
    except Exception:
        observe_provider_call(
            operation=operation,
            duration_seconds=time.monotonic() - started_at,
            error_code="PROVIDER_FAILED",
        )
        logger.exception("Exception while running DAST pipeline (pipeline_id=%s operation=%s)", pipeline_id, operation)
        record_dast_pipeline_outcome(pipeline_id, DastPipelineOutcomeCode.PROVIDER_FAILED)
        finish_pipeline(pipeline_id, degraded=True)
        raise
    observe_provider_call(operation=operation, duration_seconds=time.monotonic() - started_at)
    return _handle_dast_execution_result(task, pipeline_id, result)


def _run_sast_execution(task, pipeline_id: str):
    del task
    request = PipelineLaunchRequest.objects.get(pipeline_id=pipeline_id)
    params = dict(request.params_snapshot)
    if request.launch_config_id is not None:
        params["launch_config_id"] = request.launch_config_id
    log_level = params.get("log_level", "INFO")
    logger = install_pipeline_logging(pipeline_id, log_level)
    try:
        return _execute_sast_pipeline(
            pipeline_id,
            params,
            log_level,
            request.launch_config_id,
            logger,
            request.requester,
        )
    except Ignore:
        raise
    except Exception:
        logger.exception("Exception while running SAST pipeline (pipeline_id=%s)", pipeline_id)
        finish_pipeline(pipeline_id, degraded=True)
        raise


@dataclass(frozen=True, slots=True)
class _WorkerExecutionRuntime:
    task: object

    def run_sast(self, pipeline_id: str):
        return _run_sast_execution(self.task, pipeline_id)

    def run_dast(self, pipeline_id: str):
        return _run_dast_execution(self.task, pipeline_id, operation="execute")


@shared_task(bind=True)
def run_pipeline_execution(self, pipeline_id: str, async_user=None) -> None:
    """Execute one persisted pipeline; the broker carries only ``pipeline_id``."""
    del async_user
    execution_type = AISTPipeline.objects.values_list("execution_type", flat=True).get(pk=pipeline_id)
    try:
        driver = execution_driver_registry.resolve(execution_type)
    except UnknownLaunchAdapterError as exc:
        detail = "Persisted pipeline execution type is not executable."
        raise ValueError(detail) from exc
    acceptance = accept_published_launch(
        pipeline_id=pipeline_id,
        task_id=getattr(self.request, "id", None),
    )
    if acceptance is LaunchAcceptance.REJECTED:
        return None
    if acceptance is LaunchAcceptance.DUPLICATE:
        if not driver.allows_duplicate_delivery(
            pipeline_id=pipeline_id,
            task_id=getattr(self.request, "id", None),
            retries=int(getattr(self.request, "retries", 0) or 0),
        ):
            return None
    return driver.invoke(_WorkerExecutionRuntime(self), pipeline_id)


def _execute_sast_pipeline(pipeline_id, params, log_level, launch_config_id, logger, async_user) -> None:
    with transaction.atomic():
        pipeline = (
            AISTPipeline.objects
            .select_for_update()
            .select_related("project")
            .get(id=pipeline_id)
        )

        # Worker acceptance is the only transition into EXECUTING. Any other
        # state is either a duplicate delivery or an invalid lifecycle entry.
        if pipeline.status != AISTStatus.EXECUTING:
            logger.info("Pipeline already in progress; skipping duplicate start.")
            return

        params = PipelineArguments.from_dict(params).sast

    logger.info(f"Project version: {params.project_version}")
    param_project_version_id = (params.project_version or {}).get("id")
    if pipeline and pipeline.project_version_id and param_project_version_id:
        if int(pipeline.project_version_id) != int(param_project_version_id):
            msg = (
                "Pipeline project_version mismatch: "
                f"pipeline={pipeline.project_version_id} params={param_project_version_id}"
            )
            raise ValueError(msg)
    if params.project_version and "id" in params.project_version:
        project_version = AISTProjectVersion.objects.get(pk=params.project_version["id"])
        project_version.ensure_extracted()

    analyzers_helper = AnalyzersConfigHelper()
    project_name = params.project_name
    languages = params.languages
    project_version = params.project_version
    output_dir = params.output_dir
    rebuild_images = params.rebuild_images
    analyzers = params.analyzers
    time_class_level = params.time_class_level
    dockerfile_path = params.dockerfile_path

    ws_name = project_name or "project"
    ws_version = params.project_version.get("version", "default")
    project_build_path = get_project_build_path(ws_name, ws_version, pipeline_id)
    cleanup_terminal_project_build_paths(
        pipeline.project_id,
        ws_name,
        ws_version,
        keep_pipeline_id=pipeline_id,
    )
    # Isolate output directory per pipeline run to prevent concurrent-write collisions.
    output_dir = str(Path(output_dir) / pipeline_id)

    # Per-pipeline runtime config for agent-bridge analyzers. This goes
    # straight to the bridge runner via configure_project_run_analyses
    # (NOT through additional_environments, which is the builder
    # container's env — the bridge runs in its own container and reads
    # this from a sidecar JSON file written by agent_bridge_runner).
    agent_runtime_env = build_agent_runtime_env(pipeline)

    # Bridge client constructed once from Django settings, single source
    # of truth for socket path / timeouts (see aist/utils/bridge_client_factory.py).
    # Claude credentials (when configured for the project's org) are
    # resolved here and passed through as a generic auth_env. The
    # factory itself stays integration-agnostic (invariant I4) and
    # all Claude-specific mapping lives in aist/integrations/claude.py
    # (invariant I1).
    claude_auth = {
        var: secret.get_secret_value()
        for var, secret in claude_auth_env(pipeline.project).items()
    }
    bridge_client = build_bridge_client_from_settings(auth_env=claude_auth)

    vpn_resolved = resolve_integration(pipeline.project, OrgIntegrationType.VPN)
    logger.info("Starting configure_project_run_analyses")
    with vpn_sidecar_context(vpn_resolved, execution_id=pipeline_id) as (vpn_container, _vpn_proxy):
        if vpn_container:
            _update_action_run(
                pipeline_id,
                key="vpn_start",
                action_type="vpn",
                trigger_status="active",
                source="runner",
                status="started",
            )
        vpn_network = f"container:{vpn_container}" if vpn_container else None
        with params.script_path_context() as script_path:
            execution_result = execute_pipeline(
                PipelineExecutionType.SAST,
                SastExecutionInput(
                    execution_id=pipeline_id,
                    runtime_arguments={
                        "script_path": script_path,
                        "output_dir": output_dir,
                        "languages": languages,
                        "analyzer_config": analyzers_helper,
                        "dockerfile_path": dockerfile_path,
                        "context_dir": params.pipeline_src_path,
                        "image_name": f"project-{project_name}-builder" if project_name else "project-builder",
                        "project_path": project_build_path,
                        "force_rebuild": False,
                        "rebuild_images": rebuild_images,
                        "version": project_version,
                        "log_level": log_level,
                        "min_time_class": time_class_level or "",
                        "analyzers": analyzers,
                        "pipeline_id": pipeline_id,
                        "additional_env": params.additional_environments,
                        "network": vpn_network,
                        "bridge_client": bridge_client,
                        "agent_bridge_runtime_env": agent_runtime_env,
                    },
                ),
            )
            ld = PipelineLaunchData(execution_result.launch_data)

    ld.languages = languages
    ld.ai = {
        "mode": getattr(params, "ai_mode", "MANUAL"),
        "triage_type": getattr(params, "ai_triage_type", None),
        "filter_snapshot": getattr(params, "ai_filter_snapshot", None),
    }
    if launch_config_id:
        ld.launch_config_id = launch_config_id

    with transaction.atomic():
        pipeline = AISTPipeline.objects.select_for_update().get(id=pipeline_id)
        extra_fields: list[str] = ["launch_data"]

        if ld.resolved_commit and pipeline.project_version_id:
            resolved_version = params.resolve_effective_project_version(
                resolved_commit=ld.resolved_commit,
            )
            if resolved_version and pipeline.project_version_id != resolved_version.id:
                pipeline.project_version = resolved_version
                extra_fields.append("project_version")

        ld.merge(params.enrich_config())
        pipeline.launch_data = ld.as_dict()
        set_pipeline_status(pipeline, AISTStatus.UPLOADING_RESULTS, update_fields_extra=extra_fields)
    logger.info("Upload step starting")

    results = upload_results_internal(
        output_dir=ld.output_dir or output_dir,
        analyzers_cfg_path=ld.tmp_analyzer_config_path,
        product_name=project_name,
        repo_path=ld.project_path or project_build_path,
        trim_path=ld.trim_path,
        pipeline_id=pipeline_id,
        log_level=log_level,
    )

    raw_test_ids = [int(res.test_id) for res in (results or []) if getattr(res, "test_id", None)]
    tests: list[Test] = list(Test.objects.filter(id__in=raw_test_ids)) if raw_test_ids else []
    test_ids = [t.id for t in tests]

    finding_ids: list[int] = list(
        Finding.objects.filter(test_id__in=test_ids).values_list("id", flat=True),
    )
    with transaction.atomic():
        pipeline = AISTPipeline.objects.select_for_update().get(id=pipeline_id)
        pipeline.tests.set(tests, clear=True)
        ld = PipelineLaunchData(pipeline.launch_data)
        ld.imported_test_ids = test_ids
        pipeline.launch_data = ld.as_dict()
        pipeline.save(update_fields=["launch_data", "updated"])

    consume_analyzer_outcomes(
        pipeline_id=pipeline_id,
        outcomes=ld.analyzer_outcomes,
        import_results=results,
        output_dir=ld.output_dir or output_dir,
        user=async_user,
        logger=logger,
    )

    attach_findings_and_finish(
        pipeline_id=pipeline_id,
        project_version_id=pipeline.project_version_id,
        finding_ids=finding_ids,
        log_level=log_level,
        logger=logger,
    )
