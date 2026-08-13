"""
Canonical DAST test fixtures for both shapes of target.

A DAST target either declares a repository trigger or declares none (a perimeter target).
Every DAST test file used to define its own target dict, all of them copied from the same
source-based example, so the sourceless shape was never executed by any test. Build targets
through `TARGET_SHAPES` here and a test covers both shapes by construction.
"""

from __future__ import annotations

from dataclasses import dataclass

from django.utils import timezone

from aist.integrations.dast_config import DastTargetSnapshot
from aist.models import (
    DastIntegrationState,
    DastIntegrationValidationState,
    DastProjectBinding,
    OrgIntegration,
    OrgIntegrationType,
)
from aist.services.dast_targets import refresh_dast_targets


def parameter_schema() -> dict:
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "additionalProperties": False,
        "properties": {"depth": {"enum": ["light", "deep"]}},
        "required": ["depth"],
    }


def integration_config(public_id: str) -> dict:
    return {
        "gateway_url": "https://dast-gateway.internal",
        "ca_bundle": "",
        "contract_major": 2,
        "integrator_public_id": public_id,
        "server_fingerprint": "sha256:server-fingerprint",
    }


@dataclass(frozen=True, slots=True)
class DastTargetShape:

    """One of the two ways a provider target can declare its launch requirements."""

    label: str
    requires_repository: bool

    def wire(self, provider_id: str = "app", **overrides) -> dict:
        # Revisions and digests are fixed-width by contract, so they are derived from a truncated
        # id: a target may legitimately carry a 255-character provider id, which would otherwise
        # overflow the 96-character revision column.
        short_id = provider_id[:32]
        payload = {
            "id": provider_id,
            "display_name": f"{provider_id[:64]} API",
            "contract_revision": "2.0",
            "capability_revision": f"sha256:{short_id}-capability",
            "schema_digest": f"sha256:{short_id}-schema",
            "parameter_schema": parameter_schema(),
            "defaults": {"depth": "light"},
            "repository_keys": [provider_id, f"{provider_id}-frontend"] if self.requires_repository else [],
            "launch_requirements": ["repository-trigger"] if self.requires_repository else [],
            "autonomous_ready": True,
        }
        payload.update(overrides)
        return payload

    def source_repo_key(self, provider_id: str = "app") -> str:
        return provider_id if self.requires_repository else ""


SOURCE_BASED = DastTargetShape(label="source-based", requires_repository=True)
PERIMETER = DastTargetShape(label="perimeter", requires_repository=False)
TARGET_SHAPES = (SOURCE_BASED, PERIMETER)


def target_wire(provider_id: str = "app", **overrides) -> dict:
    """Source-based target wire payload; the default shape for tests about something else."""
    return SOURCE_BASED.wire(provider_id, **overrides)


def perimeter_target_wire(provider_id: str = "perimeter", **overrides) -> dict:
    return PERIMETER.wire(provider_id, **overrides)


def create_dast_integration(
    *,
    organization,
    public_id: str = "dast-public-id",
    name: str = "DAST integration",
    now=None,
    contract_version: str = "2.0",
) -> tuple[OrgIntegration, DastIntegrationState]:
    """Create a DAST integration already past connection validation and catalog sync."""
    checked_at = now or timezone.now()
    integration = OrgIntegration.objects.create(
        organization=organization,
        integration_type=OrgIntegrationType.DAST,
        name=name,
        config=integration_config(public_id),
        secret="runtime-token",  # noqa: S106 -- test fixture
        is_active=True,
    )
    state = DastIntegrationState.objects.create(
        integration=integration,
        validation_state=DastIntegrationValidationState.READY,
        validated_at=checked_at,
        contract_version=contract_version,
        capabilities_etag=f"{public_id}-catalog",
        capabilities_synced_at=checked_at,
    )
    return integration, state


def create_dast_target(*, integration, wire: dict, seen_at=None):
    return create_dast_targets(integration=integration, wires=(wire,), seen_at=seen_at)[0]


def create_dast_targets(*, integration, wires, seen_at=None):
    """
    Publish one catalog for this integration.

    A catalog refresh is the whole truth about an integration: any target it omits is marked
    unavailable. Tests that need several targets must publish them together, exactly as a real
    synchronization does.
    """
    return refresh_dast_targets(
        integration,
        tuple(DastTargetSnapshot.from_snapshot(wire) for wire in wires),
        seen_at=seen_at or timezone.now(),
    )


def create_dast_binding(*, project, target, parameters=None, enabled: bool = True) -> DastProjectBinding:
    """Bind a target to a project, carrying a source repository only when the target wants one."""
    binding = DastProjectBinding(
        project=project,
        target=target,
        enabled=enabled,
        parameter_snapshot=parameters if parameters is not None else {"depth": "deep"},
    )
    if binding.requires_source_repository:
        binding.source_repo_key = target.get_snapshot().repository_keys[0]
    binding.save()
    return binding
