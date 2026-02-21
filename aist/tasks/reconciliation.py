from __future__ import annotations

from celery import shared_task

from aist.utils.reconciliation import reconcile_pipeline_orphans, reconcile_recent_pipelines


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
    return reconcile_recent_pipelines(hours=hours, batch_size=batch_size, dry_run=dry_run)
