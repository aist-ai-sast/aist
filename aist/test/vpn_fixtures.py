"""VPN integrations a link must refuse, one per reason."""

from __future__ import annotations

from typing import TYPE_CHECKING

from aist.models import OrgIntegration, OrgIntegrationType

if TYPE_CHECKING:
    from aist.models import Organization


def refused_vpn_routes(organization: Organization, other_organization: Organization) -> dict[str, OrgIntegration]:
    """Integrations that may not route ``organization``'s traffic, keyed by the reason."""
    return {
        "other organization": OrgIntegration.objects.create(
            organization=other_organization,
            integration_type=OrgIntegrationType.VPN,
            name="Refused route: foreign VPN",
        ),
        "disabled VPN": OrgIntegration.objects.create(
            organization=organization,
            integration_type=OrgIntegrationType.VPN,
            name="Refused route: disabled VPN",
            is_active=False,
        ),
        "not a VPN": OrgIntegration.objects.create(
            organization=organization,
            integration_type=OrgIntegrationType.GITHUB,
            name="Refused route: GitHub posing as VPN",
        ),
    }
