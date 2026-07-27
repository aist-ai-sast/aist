from __future__ import annotations

import logging
from datetime import timedelta

from celery import shared_task
from django.db.models import Q
from django.utils import timezone

from aist.integrations.dast_capability_sync import (
    DastCapabilitySyncTicket,
    run_dast_capability_sync,
    schedule_dast_capability_sync,
)
from aist.integrations.dast_validation import DastValidationTicket, run_dast_validation
from aist.models import (
    DastIntegrationState,
    DastIntegrationValidationState,
    OrgIntegration,
    OrgIntegrationType,
)

logger = logging.getLogger(__name__)


@shared_task(name="aist.tasks.validate.validate_work_item_provider", bind=True)
def validate_work_item_provider(self, provider_id: int, async_user=None) -> dict:
    """
    Run WorkItemProvider credential validation inside a Celery worker.

    Runs in the worker process, which has Docker socket access needed for
    VPN-routed validations (vpn_sidecar_context).  Returns {"valid": bool, "detail": str}.

    _provider_id is embedded in both SUCCESS and FAILURE results so the status
    endpoint can verify the task_id belongs to the expected provider and prevent
    cross-task result disclosure.
    """
    try:
        from aist.models import WorkItemProvider  # noqa: PLC0415

        provider = WorkItemProvider.objects.select_related("vpn_integration", "vpn_integration__vpn_secret").get(
            pk=provider_id,
        )
        from aist.api.work_items import _validate_work_item_provider  # noqa: PLC0415

        valid, detail = _validate_work_item_provider(provider)
    except Exception:
        self.update_state(
            state="FAILURE",
            meta={"_provider_id": provider_id},
        )
        raise
    else:
        return {"valid": valid, "detail": detail, "_provider_id": provider_id}


@shared_task(name="aist.tasks.validate.validate_integration", bind=True)
def validate_integration(self, integration_id: int, async_user=None) -> dict:
    """
    Run integration credential validation inside a Celery worker.

    Runs in the worker process, which has Docker socket access needed for
    VPN-routed validations (vpn_sidecar_context).  Returns {"valid": bool, "detail": str}.

    _integration_id is embedded in both SUCCESS and FAILURE results so the status
    endpoint can verify the task_id belongs to the expected integration and prevent
    cross-task result disclosure.
    """
    try:
        integration = OrgIntegration.objects.select_related("vpn_integration", "vpn_secret").get(pk=integration_id)
        from aist.api.org_integrations import _validate_integration  # noqa: PLC0415

        valid, detail = _validate_integration(integration)
    except Exception:
        # Store integration_id in FAILURE meta so the status endpoint can verify
        # task ownership before returning any error details to the caller.
        self.update_state(
            state="FAILURE",
            meta={"_integration_id": integration_id},
        )
        raise
    else:
        return {"valid": valid, "detail": detail, "_integration_id": integration_id}


@shared_task(name="aist.tasks.validate.validate_dast_integration", bind=True)
def validate_dast_integration(
    self,
    integration_id: int,
    generation: int,
    async_user=None,
) -> dict:
    del async_user
    ticket = DastValidationTicket(
        integration_id=integration_id,
        generation=generation,
        task_id=self.request.id,
    )
    result = run_dast_validation(ticket)
    if result["valid"] and not result["stale"]:
        schedule_dast_capability_sync(OrgIntegration.objects.get(pk=integration_id))
    return result


@shared_task(name="aist.tasks.validate.refresh_dast_capability_catalogs", bind=True)
def refresh_dast_capability_catalogs(
    self,
    refresh_after_hours: int = 12,
    in_flight_grace_minutes: int = 30,
    async_user=None,
) -> dict:
    """
    Keep every validated DAST catalog inside the freshness window launch readiness requires.

    Readiness refuses a launch once the catalog passes `DAST_CATALOG_MAX_AGE`, so without a
    periodic refresh an installation silently stops launching a day after onboarding: schedules
    just stop producing pipelines and nothing points at the cause. Refreshing at half the maximum
    age leaves room for one failed attempt before anything is rejected.

    Each pass only reserves a generation and publishes; the sync task itself does the network
    work and an ETag makes an unchanged catalog cheap.
    """
    del self, async_user
    now = timezone.now()
    due = (
        DastIntegrationState.objects.filter(
            validation_state=DastIntegrationValidationState.READY,
            integration__is_active=True,
            integration__integration_type=OrgIntegrationType.DAST,
        )
        .filter(
            Q(capabilities_synced_at__isnull=True)
            | Q(capabilities_synced_at__lte=now - timedelta(hours=refresh_after_hours)),
        )
        .exclude(sync_claimed_at__gt=now - timedelta(minutes=in_flight_grace_minutes))
        .values_list("integration_id", flat=True)
    )

    scheduled = 0
    skipped = 0
    for integration_id in list(due):
        try:
            schedule_dast_capability_sync(OrgIntegration.objects.get(pk=integration_id))
        except (ValueError, OrgIntegration.DoesNotExist):
            # The integration stopped being READY between the query and the reservation.
            # A later pass picks it up again; a refresh is never worth failing the whole tick.
            logger.info("Skipped DAST catalog refresh for integration=%s", integration_id)
            skipped += 1
        else:
            scheduled += 1
    return {"scheduled": scheduled, "skipped": skipped}


@shared_task(name="aist.tasks.validate.sync_dast_capabilities", bind=True)
def sync_dast_capabilities(
    self,
    integration_id: int,
    generation: int,
    async_user=None,
) -> dict:
    del async_user
    ticket = DastCapabilitySyncTicket(
        integration_id=integration_id,
        generation=generation,
        task_id=self.request.id,
    )
    return run_dast_capability_sync(ticket)
