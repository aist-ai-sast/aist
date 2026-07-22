from __future__ import annotations

import logging
import shutil
import uuid
from pathlib import Path

from celery import states
from celery.result import AsyncResult
from django.conf import settings
from django.db import transaction

from aist.launch_data import PipelineLaunchData
from aist.logging_transport import uninstall_pipeline_file_logging
from aist.models import AISTPipeline, AISTStatus
from aist.signals import pipeline_finished, pipeline_status_changed
from aist.utils.pipeline_imports import cleanup_pipeline_containers
from aist.utils.reconciliation import reconcile_pipeline_orphans

_logger = logging.getLogger(__name__)
BUILD_DIR_WARNING = "AIST_PROJECTS_BUILD_DIR is not set"


def get_terminal_pipeline_statuses() -> set[str]:
    return {AISTStatus.FINISHED, AISTStatus.FINISHED_WITH_WARNINGS}


def is_terminal_pipeline_status(status: str) -> bool:
    return status in get_terminal_pipeline_statuses()


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
    old_status = pipeline.status
    if old_status == new_status:
        return False

    pipeline.status = new_status
    update_fields = {"status", "updated"}
    if is_terminal_pipeline_status(new_status):
        # Clear run_task_id on completion so the dispatcher's race-window guard
        # can distinguish completed pipelines from newly dispatched ones.
        pipeline.run_task_id = None
        update_fields.add("run_task_id")
    if update_fields_extra:
        update_fields.update(update_fields_extra)
    pipeline.save(update_fields=sorted(update_fields))

    transaction.on_commit(lambda: pipeline_status_changed.send(
        sender=type(pipeline),
        pipeline_id=pipeline.id,
        old_status=old_status,
        new_status=new_status,
    ))
    return True


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
        target_status = (
            AISTStatus.FINISHED_WITH_WARNINGS
            if degraded or launch_data_degraded or (reconcile_stats.get("remaining_violations") or 0) > 0
            else AISTStatus.FINISHED
        )
        set_pipeline_status(pipeline, target_status)
        transaction.on_commit(lambda: pipeline_finished.send(
            sender=AISTPipeline, pipeline_id=pipeline_id,
        ))
    uninstall_pipeline_file_logging(pipeline_id)


def create_pipeline_object(aist_project, project_version, pull_request, *, status: str = AISTStatus.FINISHED):
    return AISTPipeline.objects.create(
        id=uuid.uuid4().hex[:8],
        project=aist_project,
        project_version=project_version,
        pull_request=pull_request,
        status=status,
    )


def trigger_pipeline_for_pr(project, project_version, pull_request, params: dict) -> AISTPipeline:
    """
    Create an AISTPipeline for a PR event and dispatch run_sast_pipeline.

    Encapsulates pipeline creation + Celery dispatch so the GitHub event handler
    remains a thin integration layer without direct knowledge of task routing.
    """
    from aist.tasks.pipeline import run_sast_pipeline  # noqa: PLC0415

    pipeline = create_pipeline_object(project, project_version, pull_request)
    async_result = run_sast_pipeline.delay(pipeline.id, params)
    pipeline.run_task_id = async_result.id
    pipeline.save(update_fields=["run_task_id"])
    return pipeline


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
    with transaction.atomic():
        cleanup_pipeline_containers(pipeline.id)

        run_id = getattr(pipeline, "run_task_id", None)
        watch_id = getattr(pipeline, "watch_dedup_task_id", None)
        _revoke_task(run_id)
        _revoke_task(watch_id)

        pipeline.run_task_id = None
        pipeline.watch_dedup_task_id = None
    finish_pipeline(pipeline.id)
