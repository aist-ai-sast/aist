from __future__ import annotations

import logging

from celery import shared_task

logger = logging.getLogger("aist.work_items")


@shared_task(name="aist.tasks.work_items.sync_work_item_provider")
def sync_work_item_provider(provider_id: int, **_kwargs) -> dict:
    """Sync all WorkItemLinks for a single provider. Safe to retry."""
    # Import here to avoid circular imports at module load time
    from aist.models import WorkItemProvider  # noqa: PLC0415
    from aist.work_items.sync import sync_provider  # noqa: PLC0415

    try:
        provider = WorkItemProvider.objects.select_related("vpn_integration").get(pk=provider_id, is_active=True)
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


@shared_task(name="aist.tasks.work_items.sync_work_item_link")
def sync_work_item_link(link_id: int, **_kwargs) -> dict:
    """Sync a single WorkItemLink. Called after manual link creation."""
    from aist.models import WorkItemLink  # noqa: PLC0415
    from aist.work_items.sync import sync_link  # noqa: PLC0415

    try:
        link = WorkItemLink.objects.select_related("provider__vpn_integration").get(pk=link_id)
    except WorkItemLink.DoesNotExist:
        logger.warning("sync_work_item_link: link[%s] not found", link_id)
        return {"link_id": link_id, "skipped": True}

    result = sync_link(link)
    return {"link_id": link_id, "success": result.success, "error": result.error}


@shared_task(name="aist.tasks.work_items.sync_all_work_item_providers")
def sync_all_work_item_providers(**_kwargs) -> dict:
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


@shared_task(name="aist.tasks.work_items.cleanup_orphaned_vpn_containers")
def cleanup_orphaned_vpn_containers_task(max_age_minutes: int = 240, **_kwargs) -> dict:
    """
    Periodic safety-net: remove ``aist-vpn-*`` containers older than *max_age_minutes*.

    VPN sidecar containers are normally stopped by the ``finally`` block in
    ``vpn_sidecar_context``.  This task handles the edge case where a Celery
    worker was killed (SIGKILL / OOM) before the finally block could run,
    leaving orphaned containers that hold VPN connections and consume resources.
    """
    from aist.utils.vpn import cleanup_orphaned_vpn_containers  # noqa: PLC0415

    removed = cleanup_orphaned_vpn_containers(max_age_minutes=max_age_minutes)
    return {"removed": removed}
