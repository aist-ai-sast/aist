from __future__ import annotations

import logging
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

_logger = logging.getLogger(__name__)
BUILD_DIR_WARNING = "AIST_PROJECTS_BUILD_DIR is not set"


def has_unfinished_pipeline(project_version) -> bool:
    return (
        AISTPipeline.objects.filter(project_version=project_version)
        .exclude(status=AISTStatus.FINISHED)
        .exists()
    )


def get_project_build_path(project_name, project_version):
    project_build_path = getattr(settings, "AIST_PROJECTS_BUILD_DIR", None)
    if not project_build_path:
        raise RuntimeError(BUILD_DIR_WARNING)

    return str(
        Path(project_build_path) / (project_name or "project") / (project_version or "default"),
    )


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


def finish_pipeline(pipeline) -> None:
    set_pipeline_status(pipeline, AISTStatus.FINISHED)
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
