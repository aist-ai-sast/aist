from __future__ import annotations

import logging
from dataclasses import dataclass, field

from django.utils import timezone

from aist.models import WorkItemLink, WorkItemProvider
from aist.work_items.backends.base import WorkItemSyncError
from aist.work_items.backends.registry import get_backend, has_backend

logger = logging.getLogger("aist.work_items")


@dataclass
class LinkSyncResult:
    link_id: int
    success: bool
    error: str = ""


@dataclass
class ProviderSyncResult:
    provider_id: int
    synced: int = 0
    failed: int = 0
    skipped: int = 0
    errors: list[str] = field(default_factory=list)


def sync_link(link: WorkItemLink) -> LinkSyncResult:
    """
    Fetch current status for a single WorkItemLink and persist it.

    Returns a ``LinkSyncResult`` describing success or failure.
    The link is never deleted; errors are stored in ``link.sync_error``
    so operators can see what went wrong without log diving.
    """
    provider = link.provider
    if provider is None:
        # Manual links have no provider to sync from
        return LinkSyncResult(link_id=link.pk, success=False, error="manual link — no provider")

    if not (link.external_id or link.external_key or "").strip():
        error = "no external_id or external_key to fetch"
        WorkItemLink.objects.filter(pk=link.pk).update(sync_error=error)
        return LinkSyncResult(link_id=link.pk, success=False, error=error)

    try:
        backend = get_backend(provider)
        info = backend.fetch_issue_status(link)
    except NotImplementedError as exc:
        # Provider type has no sync backend (e.g. GENERIC)
        return LinkSyncResult(link_id=link.pk, success=False, error=str(exc))
    except WorkItemSyncError as exc:
        error = str(exc)
        logger.warning("WorkItemLink[%s] sync failed: %s", link.pk, error)
        WorkItemLink.objects.filter(pk=link.pk).update(
            sync_error=error,
            last_synced_at=timezone.now(),
        )
        return LinkSyncResult(link_id=link.pk, success=False, error=error)

    update_fields: dict = {
        "raw_status": info.raw_status,
        "status_category": info.status_category,
        "sync_error": "",
        "last_synced_at": timezone.now(),
    }
    if info.title:
        update_fields["title"] = info.title
    if info.external_url:
        update_fields["external_url"] = info.external_url

    WorkItemLink.objects.filter(pk=link.pk).update(**update_fields)
    logger.debug("WorkItemLink[%s] synced → %s", link.pk, info.raw_status)
    return LinkSyncResult(link_id=link.pk, success=True)


def sync_provider(provider: WorkItemProvider) -> ProviderSyncResult:
    """
    Sync all WorkItemLinks belonging to *provider*.

    Only runs if ``provider.sync_enabled`` is True and a backend exists.
    Each link is synced independently so one failure doesn't abort the rest.
    """
    result = ProviderSyncResult(provider_id=provider.pk)

    if not provider.sync_enabled:
        logger.debug("Provider[%s] sync_enabled=False — skipping", provider.pk)
        result.skipped = WorkItemLink.objects.filter(provider=provider).count()
        return result

    if not has_backend(provider.provider_type):
        logger.debug("Provider[%s] type=%s has no backend — skipping", provider.pk, provider.provider_type)
        result.skipped = WorkItemLink.objects.filter(provider=provider).count()
        return result

    links = WorkItemLink.objects.filter(provider=provider).select_related("provider")
    for link in links:
        link_result = sync_link(link)
        if link_result.success:
            result.synced += 1
        else:
            result.failed += 1
            result.errors.append(f"link[{link.pk}]: {link_result.error}")

    logger.info(
        "Provider[%s] sync complete: synced=%d failed=%d",
        provider.pk,
        result.synced,
        result.failed,
    )
    return result
