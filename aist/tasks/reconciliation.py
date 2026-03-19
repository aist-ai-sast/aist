from __future__ import annotations

import logging
import uuid
from datetime import timedelta

from celery import shared_task
from django.conf import settings
from django.db import transaction
from django.utils import timezone

from aist.models import AISTPipeline, AISTStatus
from aist.utils.reconciliation import reconcile_pipeline_orphans, reconcile_recent_pipelines

logger = logging.getLogger(__name__)

# How long a pipeline may sit in WAITING_DEDUPLICATION_TO_FINISH before the safety
# net re-dispatches its watch_deduplication task (handles Redis-down-on-commit case).
DEDUP_WATCH_RECOVERY_THRESHOLD_S = getattr(
    settings, "AIST_DEDUP_WATCH_RECOVERY_THRESHOLD_S", 1200,
)

# How long a pipeline may sit in FINDING_POSTPROCESSING before the safety net
# re-dispatches its enrich chord (handles worker crash / chord failure case).
ENRICH_STUCK_RECOVERY_THRESHOLD_S = getattr(
    settings, "AIST_ENRICH_STUCK_RECOVERY_THRESHOLD_S", 600,
)


def _recover_stuck_dedup_pipelines(*, dry_run: bool = False) -> int:
    """
    Re-dispatch watch_deduplication for pipelines stuck in WAITING_DEDUPLICATION_TO_FINISH.

    A pipeline is considered stuck when it has not progressed beyond this status for
    longer than AIST_DEDUP_WATCH_RECOVERY_THRESHOLD_S seconds. The most common cause
    is a Redis failure between the DB commit and the on_commit Celery dispatch in
    postprocess_findings().

    A new task_id is generated and saved atomically so that the recovered watcher
    can be identified and there is no collision with the original (potentially dead) task.
    """
    from aist.tasks.dedup import watch_deduplication  # noqa: PLC0415

    threshold = timezone.now() - timedelta(seconds=DEDUP_WATCH_RECOVERY_THRESHOLD_S)
    stuck = AISTPipeline.objects.filter(
        status=AISTStatus.WAITING_DEDUPLICATION_TO_FINISH,
        updated__lt=threshold,
    )
    count = 0
    for pipeline in stuck:
        if dry_run:
            count += 1
            logger.info(
                "[dry_run] Would recover stuck dedup watcher for pipeline=%s (updated=%s)",
                pipeline.id,
                pipeline.updated,
            )
            continue
        try:
            new_task_id = uuid.uuid4().hex
            with transaction.atomic():
                p = AISTPipeline.objects.select_for_update().get(id=pipeline.id)
                if p.status != AISTStatus.WAITING_DEDUPLICATION_TO_FINISH:
                    continue
                p.watch_dedup_task_id = new_task_id
                p.save(update_fields=["watch_dedup_task_id"])
                transaction.on_commit(
                    lambda pid=pipeline.id, tid=new_task_id: watch_deduplication.apply_async(
                        kwargs={"pipeline_id": pid, "log_level": "INFO"},
                        task_id=tid,
                    ),
                )
            count += 1
            logger.warning(
                "Recovered stuck dedup watcher for pipeline=%s (new_task_id=%s)",
                pipeline.id,
                new_task_id,
            )
        except Exception:
            logger.exception("Failed to recover stuck dedup pipeline=%s", pipeline.id)
    return count


def _recover_stuck_enrich_pipelines(*, dry_run: bool = False) -> int:
    """
    Re-dispatch the enrich chord for pipelines stuck in FINDING_POSTPROCESSING.

    A pipeline is considered stuck when it has not progressed beyond this status for
    longer than AIST_ENRICH_STUCK_RECOVERY_THRESHOLD_S seconds. The most common cause
    is a worker crash mid-chord that prevents the chord callback from firing.

    make_enrich_chord() re-fetches finding IDs from DB, so re-dispatch is idempotent.
    """
    from aist.tasks.enrich import make_enrich_chord  # noqa: PLC0415

    threshold = timezone.now() - timedelta(seconds=ENRICH_STUCK_RECOVERY_THRESHOLD_S)
    stuck = AISTPipeline.objects.filter(
        status=AISTStatus.FINDING_POSTPROCESSING,
        updated__lt=threshold,
    )
    count = 0
    for pipeline in stuck:
        if dry_run:
            count += 1
            logger.info(
                "[dry_run] Would re-dispatch enrich chord for pipeline=%s (updated=%s)",
                pipeline.id,
                pipeline.updated,
            )
            continue
        try:
            with transaction.atomic():
                p = AISTPipeline.objects.select_for_update().get(id=pipeline.id)
                if p.status != AISTStatus.FINDING_POSTPROCESSING:
                    continue
                transaction.on_commit(
                    lambda pid=pipeline.id: make_enrich_chord(pipeline_id=pid).apply_async(),
                )
            count += 1
            logger.warning(
                "Re-dispatched enrich chord for stuck pipeline=%s", pipeline.id,
            )
        except Exception:
            logger.exception("Failed to recover stuck enrich pipeline=%s", pipeline.id)
    return count


@shared_task(name="aist.tasks.reconciliation.reconcile_pipeline_orphans")
def reconcile_pipeline_orphans_task(pipeline_id: str, *, dry_run: bool = False, async_user=None) -> dict:
    return reconcile_pipeline_orphans(pipeline_id=pipeline_id, dry_run=dry_run)


@shared_task(name="aist.tasks.reconciliation.reconcile_recent_orphans")
def reconcile_recent_orphans_task(
    *,
    hours: int = 24,
    batch_size: int = 200,
    dry_run: bool = False,
    async_user=None,
) -> dict:
    result = reconcile_recent_pipelines(hours=hours, batch_size=batch_size, dry_run=dry_run)
    result["recovered_stuck_dedup"] = _recover_stuck_dedup_pipelines(dry_run=dry_run)
    result["recovered_stuck_enrich"] = _recover_stuck_enrich_pipelines(dry_run=dry_run)
    return result
