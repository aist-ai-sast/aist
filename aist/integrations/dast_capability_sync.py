from __future__ import annotations

import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass

from celery import current_app
from django.db import transaction
from django.utils import timezone

from aist.integrations.dast_gateway_client import DastGatewayClientError, scoped_dast_gateway_client
from aist.models import DastIntegrationState, DastIntegrationValidationState, OrgIntegration
from aist.services.dast_targets import refresh_dast_targets

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class DastCapabilitySyncTicket:

    integration_id: int
    generation: int
    task_id: str


ClientContextFactory = Callable[..., object]


def prepare_dast_capability_sync(integration: OrgIntegration) -> DastCapabilitySyncTicket:
    task_id = str(uuid.uuid4())
    with transaction.atomic():
        state = DastIntegrationState.objects.select_for_update().get(integration=integration)
        if state.validation_state != DastIntegrationValidationState.READY:
            msg = "DAST capabilities can only be synchronized for a validated integration."
            raise ValueError(msg)
        state.sync_generation += 1
        state.sync_task_id = task_id
        state.sync_claimed_at = None
        state.sync_error_code = ""
        state.save(
            update_fields=[
                "sync_generation",
                "sync_task_id",
                "sync_claimed_at",
                "sync_error_code",
                "updated",
            ],
        )
    return DastCapabilitySyncTicket(
        integration_id=integration.pk,
        generation=state.sync_generation,
        task_id=task_id,
    )


def schedule_dast_capability_sync(integration: OrgIntegration) -> DastCapabilitySyncTicket:
    """
    Reserve a sync generation and publish it once the surrounding transaction commits.

    Shared by the operator-triggered sync, the post-validation chain and the periodic refresh, so
    all three reserve a generation the same way and a stale worker result cannot overwrite a
    newer catalog. Addressed by task name to avoid importing the Celery module that imports this
    one.
    """
    ticket = prepare_dast_capability_sync(integration)
    transaction.on_commit(
        lambda: current_app.send_task(
            "aist.tasks.validate.sync_dast_capabilities",
            args=[ticket.integration_id, ticket.generation],
            task_id=ticket.task_id,
        ),
    )
    return ticket


def run_dast_capability_sync(
    ticket: DastCapabilitySyncTicket,
    *,
    client_context_factory: ClientContextFactory = scoped_dast_gateway_client,
) -> dict:
    with transaction.atomic():
        state = DastIntegrationState.objects.select_for_update().get(integration_id=ticket.integration_id)
        if not _ticket_matches(state, ticket) or state.sync_claimed_at is not None:
            return _sync_result(state, stale=True)
        if state.validation_state != DastIntegrationValidationState.READY:
            return _finish_sync_error(ticket, "INTEGRATION_NOT_READY")
        state.sync_claimed_at = timezone.now()
        state.save(update_fields=["sync_claimed_at", "updated"])
        previous_etag = state.capabilities_etag

    integration = OrgIntegration.objects.select_related(
        "vpn_integration",
        "vpn_integration__vpn_secret",
    ).get(pk=ticket.integration_id)
    try:
        with client_context_factory(
            integration,
            execution_id=f"dast-capability-sync-{ticket.task_id}",
        ) as client:
            catalog = client.catalog(etag=previous_etag)
    except DastGatewayClientError as exc:
        return _finish_sync_error(ticket, exc.code)
    except Exception:
        logger.exception("DAST capability sync[%s] failed unexpectedly", ticket.integration_id)
        return _finish_sync_error(ticket, "INTERNAL_SYNC_ERROR")

    with transaction.atomic():
        state = DastIntegrationState.objects.select_for_update().get(integration_id=ticket.integration_id)
        if not _ticket_matches(state, ticket):
            return _sync_result(state, stale=True)
        if not catalog.not_modified and catalog.etag != state.capabilities_etag:
            refresh_dast_targets(integration, catalog.targets, seen_at=timezone.now())
            state.capabilities_etag = catalog.etag
        state.capabilities_synced_at = timezone.now()
        state.sync_error_code = ""
        state.save(
            update_fields=[
                "capabilities_etag",
                "capabilities_synced_at",
                "sync_error_code",
                "updated",
            ],
        )
        return _sync_result(state)


def _finish_sync_error(ticket: DastCapabilitySyncTicket, error_code: str) -> dict:
    with transaction.atomic():
        state = DastIntegrationState.objects.select_for_update().get(integration_id=ticket.integration_id)
        if not _ticket_matches(state, ticket):
            return _sync_result(state, stale=True)
        state.sync_error_code = error_code
        state.save(update_fields=["sync_error_code", "updated"])
        return _sync_result(state)


def _ticket_matches(state: DastIntegrationState, ticket: DastCapabilitySyncTicket) -> bool:
    return state.sync_generation == ticket.generation and state.sync_task_id == ticket.task_id


def _sync_result(state: DastIntegrationState, *, stale: bool = False) -> dict:
    return {
        "_integration_id": state.integration_id,
        "generation": state.sync_generation,
        "etag": state.capabilities_etag,
        "error_code": state.sync_error_code,
        "stale": stale,
    }
