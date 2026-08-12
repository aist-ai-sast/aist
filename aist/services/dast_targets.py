from __future__ import annotations

from typing import TYPE_CHECKING

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from aist.models import DastTarget, OrgIntegration, OrgIntegrationType

if TYPE_CHECKING:
    from collections.abc import Iterable

    from aist.integrations.dast_config import DastTargetSnapshot


def refresh_dast_targets(
    integration: OrgIntegration,
    snapshots: Iterable[DastTargetSnapshot],
    *,
    seen_at=None,
) -> list[DastTarget]:
    if integration.integration_type != OrgIntegrationType.DAST:
        msg = "Target refresh requires a DAST integration."
        raise ValidationError(msg)
    parsed = list(snapshots)
    provider_ids = [snapshot.provider_id for snapshot in parsed]
    if len(provider_ids) != len(set(provider_ids)):
        msg = "DAST target refresh contains duplicate provider ids."
        raise ValidationError(msg)

    observed_at = seen_at or timezone.now()
    refreshed = []
    with transaction.atomic():
        OrgIntegration.objects.select_for_update().get(pk=integration.pk)
        for snapshot in parsed:
            wire_snapshot = snapshot.to_snapshot()
            target, _ = DastTarget.objects.update_or_create(
                integration=integration,
                provider_id=snapshot.provider_id,
                defaults={
                    "display_name": snapshot.display_name,
                    "contract_revision": snapshot.contract_revision,
                    "capability_revision": snapshot.capability_revision,
                    "schema_digest": snapshot.schema_digest,
                    "parameter_schema": wire_snapshot["parameter_schema"],
                    "provider_defaults": wire_snapshot["defaults"],
                    "repository_keys": list(snapshot.repository_keys),
                    "launch_requirements": wire_snapshot["launch_requirements"],
                    "autonomous_ready": snapshot.autonomous_ready,
                    "is_available": True,
                    "last_seen_at": observed_at,
                },
            )
            refreshed.append(target)
        stale = DastTarget.objects.filter(integration=integration).exclude(provider_id__in=provider_ids)
        stale.update(is_available=False)
    return refreshed
