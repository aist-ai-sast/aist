from __future__ import annotations

import logging

from celery import shared_task

logger = logging.getLogger("aist.work_items")


@shared_task(name="aist.tasks.work_items.sync_work_item_provider")
def sync_work_item_provider(provider_id: int) -> dict:
    """Sync all WorkItemLinks for a single provider. Safe to retry."""
    # Import here to avoid circular imports at module load time
    from aist.models import WorkItemProvider  # noqa: PLC0415
    from aist.work_items.sync import sync_provider  # noqa: PLC0415

    try:
        provider = WorkItemProvider.objects.get(pk=provider_id, is_active=True)
    except WorkItemProvider.DoesNotExist:
        logger.warning("sync_work_item_provider: provider[%s] not found or inactive", provider_id)
        return {"provider_id": provider_id, "skipped": True}

    result = sync_provider(provider)
    return {
        "provider_id": result.provider_id,
        "synced": result.synced,
        "failed": result.failed,
        "skipped": result.skipped,
    }


@shared_task(name="aist.tasks.work_items.sync_all_work_item_providers")
def sync_all_work_item_providers() -> dict:
    """
    Fan-out task dispatched by Celery Beat.

    Enqueues an individual ``sync_work_item_provider`` task for each
    active provider so they run in parallel without blocking the beat worker.
    """
    from aist.models import WorkItemProvider  # noqa: PLC0415

    providers = list(
        WorkItemProvider.objects.filter(sync_enabled=True, is_active=True).values_list("pk", flat=True),
    )
    for provider_id in providers:
        sync_work_item_provider.delay(provider_id)

    logger.info("sync_all_work_item_providers: dispatched %d provider tasks", len(providers))
    return {"dispatched": len(providers)}
