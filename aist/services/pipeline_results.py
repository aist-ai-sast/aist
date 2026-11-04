"""Shared database tail for pipeline finding imports."""

from __future__ import annotations

import uuid
from functools import partial
from typing import TYPE_CHECKING

from celery import current_app
from django.db import transaction
from dojo.models import Finding

from aist.models import AISTPipeline, AISTProjectVersion, AISTStatus, VersionType
from aist.utils.pipeline import finish_pipeline, set_pipeline_status
from aist.utils.reconciliation import safe_attach_findings_to_version

if TYPE_CHECKING:
    from aist.launch_data import PipelineLaunchData


def attach_findings_to_project_version(
    *,
    project_version_id: int | None,
    finding_ids: list[int],
    logger,
) -> list[int]:
    """Attach existing findings to the effective version and its branch parent."""
    if not finding_ids or project_version_id is None:
        return []
    with transaction.atomic():
        project_version = AISTProjectVersion.objects.select_for_update().get(id=project_version_id)
        existing_ids = list(Finding.objects.filter(id__in=finding_ids).values_list("id", flat=True))
        if not existing_ids:
            return []
        stats = safe_attach_findings_to_version(
            pv=project_version,
            finding_ids=existing_ids,
            logger=logger,
        )
        stats.log(logger=logger, pv_id=project_version.id, label="PV")
        if project_version.version_type == VersionType.GIT_HASH and project_version.resolved_from_branch_id:
            parent = AISTProjectVersion.objects.select_for_update().get(id=project_version.resolved_from_branch_id)
            parent_stats = safe_attach_findings_to_version(
                pv=parent,
                finding_ids=existing_ids,
                logger=logger,
            )
            parent_stats.log(logger=logger, pv_id=parent.id, label="Parent PV")
        return existing_ids


def record_imported_tests(pipeline: AISTPipeline, launch_data: PipelineLaunchData, tests) -> None:
    """
    Link a locked pipeline to the tests its import created and add their ids to ``imported_test_ids``.

    Reconciliation reads ``imported_test_ids`` to find tests a pipeline lost; the caller persists ``launch_data``.
    """
    tests = list(tests)
    pipeline.tests.add(*tests)
    launch_data.imported_test_ids = sorted({*launch_data.imported_test_ids, *(test.pk for test in tests)})


def schedule_pipeline_postprocessing(pipeline_id: str, log_level: str) -> None:
    """Persist the deduplication hand-off and publish it only after commit."""
    task_id = uuid.uuid4().hex
    with transaction.atomic():
        pipeline = AISTPipeline.objects.select_for_update().get(id=pipeline_id)
        pipeline.watch_dedup_task_id = task_id
        # Execution is over; the rest is async work owned by watch_dedup_task_id.
        pipeline.run_task_id = None
        set_pipeline_status(
            pipeline,
            AISTStatus.WAITING_DEDUPLICATION_TO_FINISH,
            update_fields_extra=["watch_dedup_task_id", "run_task_id"],
        )
        transaction.on_commit(partial(
            current_app.signature(
                "aist.tasks.dedup.watch_deduplication",
                kwargs={"pipeline_id": pipeline_id, "log_level": log_level},
            ).apply_async,
            task_id=task_id,
        ))


def finish_or_schedule_pipeline_results(
    *,
    pipeline_id: str,
    finding_ids: list[int],
    log_level: str,
    logger,
) -> None:
    """Finish a clean import or start the shared deduplication stage."""
    if not finding_ids:
        logger.info("No findings to enrich; Finishing pipeline")
        finish_pipeline(pipeline_id)
        return
    schedule_pipeline_postprocessing(pipeline_id, log_level)


def attach_findings_and_finish(
    *,
    pipeline_id: str,
    project_version_id: int | None,
    finding_ids: list[int],
    log_level: str,
    logger,
) -> None:
    """Attach imported findings to their project version, then finish or start deduplication."""
    finish_or_schedule_pipeline_results(
        pipeline_id=pipeline_id,
        finding_ids=attach_findings_to_project_version(
            project_version_id=project_version_id,
            finding_ids=finding_ids,
            logger=logger,
        ),
        log_level=log_level,
        logger=logger,
    )
