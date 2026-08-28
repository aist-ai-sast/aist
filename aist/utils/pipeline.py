from __future__ import annotations

import logging
import shutil
import uuid
from pathlib import Path

from celery import states
from celery.result import AsyncResult
from django.conf import settings
from django.db import transaction
from django.utils import timezone

from aist.execution.contracts import ExecutionCancellationMode
from aist.execution.observability import AuditContext, audit_event
from aist.execution.registry import execution_driver_registry
from aist.launch_data import PipelineLaunchData
from aist.logging_transport import uninstall_pipeline_file_logging
from aist.models import (
    AISTPipeline,
    AISTStatus,
    DastExecutionOutcome,
    DastExecutionState,
    DastRunMetadata,
    PipelineExecutionType,
    PipelineLaunchRequest,
    PipelineLaunchRequestState,
)
from aist.services.pipeline_lifecycle import (
    TERMINAL_PIPELINE_STATUSES,
    is_terminal_pipeline_status,
    transition_pipeline_status,
)
from aist.signals import pipeline_finished
from aist.utils.pipeline_imports import cleanup_pipeline_containers
from aist.utils.reconciliation import reconcile_pipeline_orphans

_logger = logging.getLogger(__name__)
BUILD_DIR_WARNING = "AIST_PROJECTS_BUILD_DIR is not set"


def get_terminal_pipeline_statuses() -> set[str]:
    return set(TERMINAL_PIPELINE_STATUSES)


def has_unfinished_pipeline(project_version) -> bool:
    return (
        AISTPipeline.objects.filter(project_version=project_version)
        .exclude(status__in=get_terminal_pipeline_statuses())
        .exists()
    )


def get_project_build_path(project_name: str, project_version: str, pipeline_id: str) -> str:
    """
    Return an isolated workspace path for a single pipeline run.

    Path structure: <AIST_PROJECTS_BUILD_DIR>/<project_name>/<project_version>/runs/<pipeline_id>
    Each run gets its own directory, eliminating concurrent-checkout races.

    Raises ValueError if the computed path escapes the build directory (path traversal guard).
    """
    project_build_path = getattr(settings, "AIST_PROJECTS_BUILD_DIR", None)
    if not project_build_path:
        raise RuntimeError(BUILD_DIR_WARNING)

    base = Path(project_build_path).resolve()
    run_dir = (
        base
        / (project_name or "project")
        / (project_version or "default")
        / "runs"
        / pipeline_id
    )
    resolved = run_dir.resolve()
    try:
        resolved.relative_to(base)
    except ValueError:
        msg = f"Workspace path escapes build directory: {resolved}"
        raise ValueError(msg)
    return str(resolved)


def _remove_pipeline_workspace(project_name: str, project_version: str, pipeline_id: str) -> None:
    project_build_path = getattr(settings, "AIST_PROJECTS_BUILD_DIR", None)
    if not project_build_path:
        return
    run_dir = (
        Path(project_build_path)
        / (project_name or "project")
        / (project_version or "default")
        / "runs"
        / pipeline_id
    )
    if run_dir.exists():
        shutil.rmtree(run_dir)
        _logger.info("Cleaned up pipeline workspace: %s", run_dir)


def cleanup_project_build_path(project_name: str, project_version: str, pipeline_id: str) -> None:
    """Remove the per-pipeline workspace directory created by get_project_build_path."""
    try:
        _remove_pipeline_workspace(project_name, project_version, pipeline_id)
    except Exception:
        _logger.exception("Failed to clean up pipeline workspace (pipeline_id=%s)", pipeline_id)


def cleanup_terminal_project_build_paths(
    project_id: int,
    project_name: str,
    project_version: str,
    *,
    keep_pipeline_id: str,
) -> None:
    """
    Remove stale per-run workspaces for finished pipelines in the same project/version workspace.

    The currently starting pipeline must always be preserved to avoid deleting the active
    workspace during duplicate task delivery or concurrent launches.

    Uses SELECT FOR UPDATE SKIP LOCKED so concurrent pipeline starts don't race on the
    same rows — only one caller claims a batch of terminal pipelines at a time.
    """
    with transaction.atomic():
        terminal_pipeline_ids = list(
            AISTPipeline.objects.select_for_update(skip_locked=True)
            .filter(
                project_id=project_id,
                status__in=get_terminal_pipeline_statuses(),
            )
            .exclude(id=keep_pipeline_id)
            .values_list("id", flat=True),
        )
    # Filesystem cleanup happens outside the transaction to avoid
    # holding DB locks during potentially slow shutil.rmtree calls.
    for pipeline_id in terminal_pipeline_ids:
        cleanup_project_build_path(project_name, project_version, pipeline_id)


def set_pipeline_status(
    pipeline: AISTPipeline,
    new_status: str,
    *,
    update_fields_extra: list[str] | None = None,
) -> bool:
    extras = update_fields_extra or []
    result = transition_pipeline_status(
        pipeline.pk,
        new_status,
        field_updates={field: getattr(pipeline, field) for field in extras},
        update_fields=extras,
    )
    pipeline.status = result.pipeline.status
    pipeline.started = result.pipeline.started
    pipeline.finished_at = result.pipeline.finished_at
    pipeline.run_task_id = result.pipeline.run_task_id
    return result.changed


def finish_pipeline(pipeline_id: str, *, degraded: bool = False) -> None:
    """
    Transition pipeline to a terminal status (FINISHED or FINISHED_WITH_WARNINGS).

    Two independent phases so that a reconciliation failure never prevents the status
    update, and a status-update failure never propagates to the caller:

    Phase 1 - reconciliation: runs its own transactions; exceptions are absorbed and
    recorded as violations so the pipeline lands in FINISHED_WITH_WARNINGS instead of
    staying stuck.

    Phase 2 - status save: opens its own transaction.atomic() with a fresh SELECT FOR
    UPDATE so the pipeline object is never stale, even when called from an exception
    handler.  All exceptions are absorbed and logged.
    """
    # Phase 1: reconciliation - independent transactions managed by reconcile_pipeline_orphans
    try:
        reconcile_stats = reconcile_pipeline_orphans(pipeline_id=pipeline_id, dry_run=False, logger=_logger)
    except Exception:
        _logger.exception("Pipeline reconciliation failed (pipeline_id=%s)", pipeline_id)
        reconcile_stats = {"remaining_violations": 1}

    # Phase 2: status update in its own independent transaction
    try:
        _finalize_pipeline_status(pipeline_id, degraded=degraded, reconcile_stats=reconcile_stats)
    except Exception:
        _logger.exception("Failed to set terminal status (pipeline_id=%s)", pipeline_id)


def _finalize_pipeline_status(pipeline_id: str, *, degraded: bool, reconcile_stats: dict) -> None:
    with transaction.atomic():
        pipeline = AISTPipeline.objects.select_for_update().get(id=pipeline_id)
        if is_terminal_pipeline_status(pipeline.status):
            # Already finished - skip to avoid overwriting a terminal status
            # (e.g. FINISHED -> FINISHED_WITH_WARNINGS on a stale degraded=True call).
            return
        launch_data_degraded = PipelineLaunchData(pipeline.launch_data).has_analyzer_degraded_reasons
        try:
            dast_delivery_degraded = pipeline.dast_run_metadata.delivery_quality in {"degraded", "partial"}
        except DastRunMetadata.DoesNotExist:
            dast_delivery_degraded = False
        target_status = (
            AISTStatus.FINISHED_WITH_WARNINGS
            if degraded or launch_data_degraded or dast_delivery_degraded
            or (reconcile_stats.get("remaining_violations") or 0) > 0
            else AISTStatus.FINISHED
        )
        set_pipeline_status(pipeline, target_status)
        transaction.on_commit(lambda: pipeline_finished.send(
            sender=AISTPipeline, pipeline_id=pipeline_id,
        ))
    uninstall_pipeline_file_logging(pipeline_id)


def create_pipeline_object(
    aist_project,
    project_version,
    pull_request,
    *,
    status: str = AISTStatus.ADMITTED,
    execution_type: str = PipelineExecutionType.SAST,
    trigger_project_version=None,
):
    return AISTPipeline.objects.create(
        id=uuid.uuid4().hex[:8],
        project=aist_project,
        project_version=project_version,
        trigger_project_version=trigger_project_version,
        execution_type=execution_type,
        pull_request=pull_request,
        status=status,
    )


def _revoke_task(task_id: str | None, *, terminate: bool = True) -> None:
    """Safely revoke a Celery task by its ID if it is still running."""
    if not task_id:
        return
    try:
        result = AsyncResult(task_id)
        if result.state not in states.READY_STATES:
            result.revoke(terminate=terminate)
    except Exception:
        _logger.exception("Failed to revoke Celery task: %s", task_id)


def stop_pipeline(pipeline: AISTPipeline) -> None:
    """
    Stop all Celery tasks associated with an AISTPipeline.

    Revokes both the run and deduplication watcher tasks (if present),
    tears down any related containers.
    """
    driver = execution_driver_registry.resolve(pipeline.execution_type)
    if driver.cancellation_mode == ExecutionCancellationMode.COOPERATIVE:
        _request_dast_pipeline_stop(pipeline.id)
        return

    with transaction.atomic():
        cleanup_pipeline_containers(pipeline.id)

        run_id = getattr(pipeline, "run_task_id", None)
        watch_id = getattr(pipeline, "watch_dedup_task_id", None)
        _revoke_task(run_id)
        _revoke_task(watch_id)

        pipeline.run_task_id = None
        pipeline.watch_dedup_task_id = None
    finish_pipeline(pipeline.id)


def _request_dast_pipeline_stop(pipeline_id: str) -> None:
    """Persist DAST cancellation before signalling its connector container."""
    finish_without_provider = False
    task_id = None
    with transaction.atomic():
        launch_request = (
            PipelineLaunchRequest.objects
            .select_for_update()
            .filter(pipeline_id=pipeline_id)
            .first()
        )
        pipeline = AISTPipeline.objects.select_for_update().get(pk=pipeline_id)
        if is_terminal_pipeline_status(pipeline.status):
            return
        execution_state = DastExecutionState.objects.select_for_update().get(pipeline=pipeline)
        if execution_state.cancel_requested_at is None:
            execution_state.cancel_requested_at = timezone.now()
        execution_state.outcome = DastExecutionOutcome.STOP_PENDING
        execution_state.save(update_fields=["cancel_requested_at", "outcome", "updated"])
        task_id = pipeline.run_task_id
        # A stop reaches the provider only through the connector, so cancellation stays
        # cooperative while an execution owns this pipeline. It completes locally when nobody
        # does: either the request was never dispatched, or no worker holds it and no provider
        # run exists to cancel -- otherwise Stop would wait on a connector that never starts.
        cancelled_before_dispatch = (
            launch_request is not None
            and launch_request.state != PipelineLaunchRequestState.DISPATCHED
        )
        nothing_to_cancel_remotely = not execution_state.run_id and not pipeline.run_task_id
        if cancelled_before_dispatch or nothing_to_cancel_remotely:
            if launch_request is not None:
                launch_request.state = PipelineLaunchRequestState.CANCELLED
                launch_request.save(update_fields=["state", "updated"])
            execution_state.outcome = DastExecutionOutcome.CANCELLED_BEFORE_START
            execution_state.save(update_fields=["outcome", "updated"])
            finish_without_provider = True
            transaction.on_commit(lambda: cleanup_pipeline_containers(pipeline_id))
        else:
            transaction.on_commit(lambda: cleanup_pipeline_containers(pipeline_id))
        transaction.on_commit(
            lambda: audit_event(
                "dast_cancel_requested",
                context=AuditContext(
                    organization_id=pipeline.project.organization_id,
                    project_id=pipeline.project_id,
                    integration_id=(
                        launch_request.dast_binding.target.integration_id
                        if launch_request is not None and launch_request.dast_binding_id is not None
                        else None
                    ),
                    binding_id=launch_request.dast_binding_id if launch_request is not None else None,
                    request_id=launch_request.pk if launch_request is not None else None,
                    pipeline_id=pipeline.id,
                ),
            ),
        )

    if finish_without_provider:
        _revoke_task(task_id)
        finish_pipeline(pipeline_id, degraded=True)
