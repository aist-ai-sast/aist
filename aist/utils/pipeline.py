from __future__ import annotations

import logging
import shutil
import uuid
from pathlib import Path

from celery import states
from celery.result import AsyncResult
from django.conf import settings
from django.db import transaction

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
    """
    project_build_path = getattr(settings, "AIST_PROJECTS_BUILD_DIR", None)
    if not project_build_path:
        raise RuntimeError(BUILD_DIR_WARNING)

    return str(
        Path(project_build_path)
        / (project_name or "project")
        / (project_version or "default")
        / "runs"
        / pipeline_id,
    )


def cleanup_project_build_path(project_name: str, project_version: str, pipeline_id: str) -> None:
    """Remove the per-pipeline workspace directory created by get_project_build_path."""
    try:
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
    except Exception:
        _logger.exception("Failed to clean up pipeline workspace (pipeline_id=%s)", pipeline_id)


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


def finish_pipeline(pipeline, *, degraded: bool = False) -> None:
    try:
        reconcile_stats = reconcile_pipeline_orphans(pipeline_id=pipeline.id, dry_run=False, logger=_logger)
    except Exception:
        _logger.exception("Pipeline reconciliation failed (pipeline_id=%s)", pipeline.id)
        reconcile_stats = {"remaining_violations": 1}
    target_status = (
        AISTStatus.FINISHED_WITH_WARNINGS
        if degraded or (reconcile_stats.get("remaining_violations") or 0) > 0
        else AISTStatus.FINISHED
    )
    set_pipeline_status(pipeline, target_status)
    transaction.on_commit(lambda: pipeline_finished.send(
        sender=type(pipeline), pipeline_id=pipeline.id,
    ))
    uninstall_pipeline_file_logging(pipeline.id)


def create_pipeline_object(aist_project, project_version, pull_request):
    return AISTPipeline.objects.create(
        id=uuid.uuid4().hex[:8],
        project=aist_project,
        project_version=project_version,
        pull_request=pull_request,
        status=AISTStatus.FINISHED,
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
    with transaction.atomic():
        cleanup_pipeline_containers(pipeline.id)

        run_id = getattr(pipeline, "run_task_id", None)
        watch_id = getattr(pipeline, "watch_dedup_task_id", None)
        _revoke_task(run_id)
        _revoke_task(watch_id)

        pipeline.run_task_id = None
        pipeline.watch_dedup_task_id = None
        finish_pipeline(pipeline)
