from __future__ import annotations

import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass

from celery import current_app
from django.conf import settings
from django.db import transaction
from django.utils import timezone

from aist.execution.observability import operational_alert
from aist.integrations.dast_gateway_client import (
    DastGatewayClient,
    DastGatewayClientError,
    scoped_dast_gateway_client,
)
from aist.models import (
    DastIntegrationState,
    DastIntegrationValidationState,
    OrgIntegration,
    OrgIntegrationType,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class DastValidationTicket:

    integration_id: int
    generation: int
    task_id: str


ClientContextFactory = Callable[..., object]

# How long a scheduled validation waits before the worker may pick it up. Long enough that a
# burst of connection writes collapses into one gateway probe (later generations supersede the
# earlier ones), short enough that an operator watching the UI still sees a result promptly.
VALIDATION_DEBOUNCE_SECONDS = getattr(settings, "AIST_DAST_VALIDATION_DEBOUNCE_SECONDS", 10)


def mark_dast_validation_pending(integration: OrgIntegration) -> DastIntegrationState:
    if integration.integration_type != OrgIntegrationType.DAST:
        msg = "Only DAST integrations have DAST validation state."
        raise ValueError(msg)
    with transaction.atomic():
        state, _created = DastIntegrationState.objects.select_for_update().get_or_create(integration=integration)
        state.validation_generation += 1
        state.validation_state = DastIntegrationValidationState.PENDING_VALIDATION
        state.validation_task_id = ""
        state.validation_claimed_at = None
        state.validation_error_code = ""
        state.contract_version = ""
        state.validated_at = None
        state.save(
            update_fields=[
                "validation_generation",
                "validation_state",
                "validation_task_id",
                "validation_claimed_at",
                "validation_error_code",
                "contract_version",
                "validated_at",
                "updated",
            ],
        )
        return state


def mark_vpn_linked_dast_validations_pending(vpn_integration: OrgIntegration) -> None:
    integration_ids = OrgIntegration.objects.filter(
        integration_type=OrgIntegrationType.DAST,
        vpn_integration=vpn_integration,
    ).values_list("id", flat=True)
    for integration_id in integration_ids.iterator():
        integration = OrgIntegration.objects.get(pk=integration_id)
        mark_dast_validation_pending(integration)


def prepare_dast_validation(integration: OrgIntegration) -> DastValidationTicket:
    state = mark_dast_validation_pending(integration)
    task_id = str(uuid.uuid4())
    with transaction.atomic():
        state = DastIntegrationState.objects.select_for_update().get(pk=state.pk)
        state.validation_state = DastIntegrationValidationState.VALIDATING
        state.validation_task_id = task_id
        state.save(update_fields=["validation_state", "validation_task_id", "updated"])
    return DastValidationTicket(
        integration_id=integration.pk,
        generation=state.validation_generation,
        task_id=task_id,
    )


def schedule_dast_validation(integration: OrgIntegration) -> DastValidationTicket:
    """
    Reserve a validation generation and publish it once the surrounding transaction commits.

    Every producer goes through here -- onboarding import, bundle replacement, token rotation and
    the explicit validate endpoint -- so an integration can never be left in
    `PENDING_VALIDATION` with no task behind it. The task is addressed by name rather than
    imported to keep this module free of a dependency on the Celery task module that imports it.

    Publication is deliberately delayed by `VALIDATION_DEBOUNCE`. Validation is the one step that
    reaches out over the network, and several cheap writes can each mean "the stored connection
    changed" -- toggling `is_active`, re-saving a bundle, rotating a token. Without the delay a
    burst of those becomes a burst of gateway calls. With it, each write still reserves a fresh
    generation, but only the last one is still current when the worker picks it up; the earlier
    tasks see a superseded generation in `run_dast_validation` and return before opening a
    connection. The net effect is at most one probe per integration per debounce window, and the
    connection that finally gets validated is always the one that is stored.
    """
    ticket = prepare_dast_validation(integration)
    # The state was written through a separately loaded row. Drop the caller's cached relation so
    # a response rendered from this instance reports the state that is actually stored, rather
    # than the one that was true before this call.
    integration.refresh_from_db()
    transaction.on_commit(
        lambda: current_app.send_task(
            "aist.tasks.validate.validate_dast_integration",
            args=[ticket.integration_id, ticket.generation],
            task_id=ticket.task_id,
            countdown=VALIDATION_DEBOUNCE_SECONDS,
        ),
    )
    return ticket


def run_dast_validation(
    ticket: DastValidationTicket,
    *,
    client_context_factory: ClientContextFactory = scoped_dast_gateway_client,
) -> dict:
    """Claim, perform, and persist one validation without holding a DB lock during network I/O."""
    with transaction.atomic():
        state = DastIntegrationState.objects.select_for_update().select_related("integration").get(
            integration_id=ticket.integration_id,
        )
        if not _ticket_matches(state, ticket) or state.validation_claimed_at is not None:
            return _validation_result(state, stale=True)
        state.validation_claimed_at = timezone.now()
        state.save(update_fields=["validation_claimed_at", "updated"])

    integration = OrgIntegration.objects.select_related(
        "vpn_integration",
        "vpn_integration__vpn_secret",
    ).get(pk=ticket.integration_id)
    if not integration.is_active:
        return _finish_validation(ticket, error_code="INTEGRATION_DISABLED")
    try:
        with client_context_factory(
            integration,
            execution_id=f"dast-validation-{ticket.task_id}",
        ) as client:
            if not isinstance(client, DastGatewayClient):
                # Test doubles may provide the same typed methods without inheriting the concrete client.
                logger.debug("DAST validation[%s] uses an injected client", ticket.integration_id)
            ping = client.ping()
    except DastGatewayClientError as exc:
        return _finish_validation(ticket, error_code=exc.code)
    except Exception:
        logger.exception("DAST validation[%s] failed unexpectedly", ticket.integration_id)
        return _finish_validation(ticket, error_code="INTERNAL_VALIDATION_ERROR")
    return _finish_validation(ticket, contract_version=ping.contract_version)


def _finish_validation(
    ticket: DastValidationTicket,
    *,
    contract_version: str = "",
    error_code: str = "",
) -> dict:
    with transaction.atomic():
        state = DastIntegrationState.objects.select_for_update().get(integration_id=ticket.integration_id)
        if not _ticket_matches(state, ticket):
            return _validation_result(state, stale=True)
        state.validation_state = (
            DastIntegrationValidationState.INVALID if error_code else DastIntegrationValidationState.READY
        )
        state.validation_error_code = error_code
        state.contract_version = contract_version
        state.validated_at = timezone.now()
        state.save(
            update_fields=[
                "validation_state",
                "validation_error_code",
                "contract_version",
                "validated_at",
                "updated",
            ],
        )
        if error_code:
            transaction.on_commit(
                lambda: operational_alert(code="validation_failed", execution_type="dast", count=1),
            )
        return _validation_result(state)


def _ticket_matches(state: DastIntegrationState, ticket: DastValidationTicket) -> bool:
    return (
        state.validation_generation == ticket.generation
        and state.validation_task_id == ticket.task_id
    )


def _validation_result(state: DastIntegrationState, *, stale: bool = False) -> dict:
    return {
        "_integration_id": state.integration_id,
        "generation": state.validation_generation,
        "state": state.validation_state,
        "valid": state.validation_state == DastIntegrationValidationState.READY,
        "error_code": state.validation_error_code,
        "stale": stale,
    }
